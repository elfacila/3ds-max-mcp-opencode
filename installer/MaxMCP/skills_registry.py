"""
Extensible Skills framework for the 3ds Max MCP bridge (3ds Max 2022 / Py3.7).

Design goals:
  * Decoupled from transport: skills know nothing about sockets or Qt.
  * Dual execution: the SAME function runs via the MCP socket dispatch
    AND directly from the UI ("Execute Locally" button).
  * Registry pattern: developers add a skill with one decorator::

        from skills_registry import register_skill

        @register_skill(
            name="my_tool",
            category="Scene Management",
            description="Does something useful.",
            params=[
                {"name": "count", "type": "int", "default": 1,
                 "label": "Count", "min": 1, "max": 10},
            ],
        )
        def my_tool(count=1):
            ...
            return {"ok": True, ...}   # must be JSON serializable

  * User-managed skills without editing sources: drop .py files using the
    decorator above into <module>/custom_skills/ (== #userScripts/MaxMCP/
    custom_skills/ when installed).  They are imported automatically at
    startup and hot-reloaded by reload_skills(), which the UI exposes via
    Add / Import / Reload buttons.  Parameter types: str, int, float,
    bool, choice, node (scene node referenced by name).

  * Remote (MCP) dispatch: the outside MCP server (or the bundled GUI
    listener) never imports this file remotely.  Instead it sends code::

        import skills_registry
        __mcp_result__ = skills_registry.execute_skill("my_tool", {...})

    Use :func:`build_skill_code` to generate that snippet.

Python 3.7 compatible (no union syntax, no match, no dataclasses).
No third-party dependencies: stdlib + pymxs (guarded).
"""

import ast
import importlib.util
import json
import os
import sys
import traceback

try:
    from pymxs import runtime as rt
    _IN_MAX = True
except Exception:
    rt = None
    _IN_MAX = False

SKILLS_VERSION = "1.2.0"

# name -> SkillSpec
SKILL_REGISTRY = {}

# Custom (user-managed) skills. Directory override for tests/embedders:
#   MAXMCP_CUSTOM_SKILLS=/path/to/dir
CUSTOM_SKILLS_ENV = "MAXMCP_CUSTOM_SKILLS"
# custom module name -> loaded .py path (builtin skills are not tracked here)
_SKILL_MODULE_FILES = {}

# Live enablement + profile state (see "Profiles & enablement" section).
PROFILES_ENV = "MAXMCP_PROFILES"
PROFILES_FILENAME = "profiles_config.json"
PROFILE_ALL = "All Skills"
PROFILE_SHELL = "Floor Plan Shell"
PROFILE_MINIMAL = "Minimal / None"
# Categories composing the default "Floor Plan Shell" profile
# (modeling + spline cleanup; camera/lighting excluded).
SHELL_CATEGORIES = ("Scene Management", "Geometry & Modifiers")
_ENABLED = set()          # live enabled skill names; default = everything
_PROFILES = None          # lazy {"active_profile": str, "profiles": {...}}
_PROFILES_ERROR = None    # last profiles-file problem (surfaced in UI)
_TOOLS_VERSION = [0]      # bumped on every advertised-catalog change
_TOOLS_CHANGED_CBS = []   # fn(version, enabled_count); never raises out


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------
VALID_PARAM_TYPES = ("str", "int", "float", "bool", "choice", "node")


def _coerce_value(spec, value):
    """Coerce one raw value to the type declared in its param spec."""
    ptype = spec.get("type", "str")
    if value is None:
        return spec.get("default")
    try:
        if ptype == "str":
            return str(value)
        if ptype == "int":
            coerced = int(float(value)) if isinstance(value, str) else int(value)
            if "min" in spec and spec["min"] is not None:
                coerced = max(int(spec["min"]), coerced)
            if "max" in spec and spec["max"] is not None:
                coerced = min(int(spec["max"]), coerced)
            return coerced
        if ptype == "float":
            coerced = float(value)
            if "min" in spec and spec["min"] is not None:
                coerced = max(float(spec["min"]), coerced)
            if "max" in spec and spec["max"] is not None:
                coerced = min(float(spec["max"]), coerced)
            return coerced
        if ptype == "bool":
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "y", "on")
            return bool(value)
        if ptype == "choice":
            choices = list(spec.get("choices") or [])
            if choices and value not in choices:
                # Case-insensitive fallback for convenience.
                lowered = str(value).lower()
                for choice in choices:
                    if str(choice).lower() == lowered:
                        return choice
                return spec.get("default")
            return value
        if ptype == "node":
            # Scene-node reference, carried by name. Resolved at call time
            # inside the skill body (never here, so headless use is safe).
            return str(value)
    except Exception:
        return spec.get("default")
    return value


def validate_params(skill_name, params):
    """Merge user params over schema defaults with type coercion.

    Returns a plain dict of {param_name: coerced_value}.
    Raises KeyError for unknown skills, ValueError for bad input types.
    """
    spec = SKILL_REGISTRY.get(skill_name)
    if spec is None:
        raise KeyError("Unknown skill: %r. Available: %s"
                       % (skill_name, sorted(SKILL_REGISTRY)))
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params must be a dict, got %r" % type(params))
    coerced = {}
    for pdef in spec.params:
        pname = pdef["name"]
        raw = params.get(pname, pdef.get("default"))
        coerced[pname] = _coerce_value(pdef, raw)
    return coerced


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
class SkillSpec(object):
    """Metadata + callable for one skill."""

    def __init__(self, name, category, description, params, func,
                 title=None, source="builtin"):
        self.name = name
        self.title = title or name
        self.category = category or "General"
        self.description = description or ""
        self.params = list(params or [])
        self.func = func
        # "builtin" or the custom module name (see _SKILL_MODULE_FILES).
        self.source = source or "builtin"

    def to_dict(self):
        return {
            "name": self.name,
            "title": self.title,
            "category": self.category,
            "description": self.description,
            "params": [dict(p) for p in self.params],
            "source": self.source,
            "enabled": self.name in _ENABLED,
        }

    def __repr__(self):
        return "SkillSpec(%r, category=%r, source=%r)" % (
            self.name, self.category, self.source)


def register_skill(name, category="General", description="", params=None,
                   title=None):
    """Decorator registering a function as a skill.

    Usage::

        @register_skill(name="scene_cleaner",
                         category="Scene Management",
                         description="...",
                         params=[{"name": "dry_run", "type": "bool",
                                  "default": False, "label": "Dry run"}])
        def scene_cleaner(dry_run=False):
            return {"ok": True}

    The skill's ``source`` is attributed automatically: functions defined
    in this module are "builtin", functions imported from a file under
    :func:`custom_skills_dir` carry that file's module name.
    """
    if not name or not isinstance(name, str):
        raise ValueError("Skill 'name' must be a non-empty string.")
    param_defs = list(params or [])
    for pdef in param_defs:
        if "name" not in pdef:
            raise ValueError("Every param needs a 'name': %r" % (pdef,))
        if pdef.get("type", "str") not in VALID_PARAM_TYPES:
            raise ValueError("Param %r has invalid type %r. Valid: %s"
                             % (pdef.get("name"), pdef.get("type"),
                                VALID_PARAM_TYPES))
        if pdef.get("type") == "choice" and not pdef.get("choices"):
            raise ValueError("Param %r of type 'choice' needs 'choices'."
                             % pdef.get("name"))

    def _decorator(func):
        if name in SKILL_REGISTRY:
            raise ValueError("Skill %r is already registered." % name)
        module = getattr(func, "__module__", "") or ""
        source = "builtin" if module == __name__ else module
        SKILL_REGISTRY[name] = SkillSpec(
            name=name,
            category=category,
            description=description,
            params=param_defs,
            func=func,
            title=title,
            source=source,
        )
        return func

    return _decorator


def list_skills(enabled_only=False):
    """Return skill metadata dicts sorted by (category, name)."""
    names = sorted(SKILL_REGISTRY,
                   key=lambda n: (SKILL_REGISTRY[n].category,
                                  SKILL_REGISTRY[n].name))
    if enabled_only:
        names = [n for n in names if n in _ENABLED]
    return [SKILL_REGISTRY[k].to_dict() for k in names]


def list_enabled_skills():
    """Return metadata dicts for currently enabled skills only."""
    return list_skills(enabled_only=True)


def skills_by_category(enabled_only=False):
    """Return {category: [skill_dict, ...]} with skills sorted by name."""
    grouped = {}
    for skill in list_skills(enabled_only=enabled_only):
        grouped.setdefault(skill["category"], []).append(skill)
    return grouped


def get_skill(name):
    """Return the SkillSpec for *name* (or None)."""
    return SKILL_REGISTRY.get(name)


def execute_skill(name, params=None, enforce_enabled=True):
    """Run a skill by name with a params dict. Dual entry point.

    Called directly from the UI thread AND from MCP-dispatched code
    running in the main 3ds Max thread.  Always returns a JSON-safe dict.
    Never raises: failures are returned as {"ok": False, "error": ...}.

    With enforce_enabled=True (default: every remote/MCP path), skills
    disabled by the active profile are refused with a clear error so the
    advertised catalog and dispatch can never disagree.  The UI passes
    enforce_enabled=False for an explicit local click, but logs the
    override (see max_mcp_ui.on_execute_locally).
    """
    try:
        spec = SKILL_REGISTRY.get(name)
        if spec is None:
            return {"ok": False,
                    "skill": name,
                    "error": "Unknown skill %r. Available: %s"
                             % (name, sorted(SKILL_REGISTRY))}
        if enforce_enabled and name not in _ENABLED:
            return {"ok": False,
                    "skill": name,
                    "error": "Skill %r is disabled by the active profile "
                             "%r. Enable it in the Skill Manager." % (
                                 name, get_active_profile())}
        coerced = validate_params(name, params or {})
    except Exception as exc:
        return {"ok": False, "skill": name,
                "error": "Parameter error: %s" % exc}
    try:
        result = spec.func(**coerced)
        if result is None:
            result = {"ok": True, "skill": name}
        if isinstance(result, dict):
            result.setdefault("skill", name)
            if "ok" not in result:
                result["ok"] = True
            try:
                json.dumps(result)
                return result
            except Exception:
                return {"ok": True, "skill": name,
                        "result_repr": repr(result)}
        try:
            json.dumps(result)
            return {"ok": True, "skill": name, "result": result}
        except Exception:
            return {"ok": True, "skill": name,
                    "result_repr": repr(result)}
    except Exception:
        return {"ok": False, "skill": name,
                "error": traceback.format_exc()}


def build_skill_code(name, params=None):
    """Build the pymxs snippet a remote MCP client sends to run a skill.

    Compatible with the existing newline-delimited {"id","code"} socket
    protocol used by max_listener.py / max_mcp_ui.py, e.g.::

        send_to_max(build_skill_code("scene_cleaner", {"dry_run": True}))
    """
    payload = json.dumps(params or {}, sort_keys=True)
    # NOTE: runs inside 3ds Max (Python 3.7): keep it simple, no f-strings
    # needed here but they would work; % formatting is version-proof.
    code = (
        "import skills_registry\n"
        "__mcp_result__ = skills_registry.execute_skill(%r, %s)\n"
        "print(\"skill %s -> ok=%%s\" %% __mcp_result__.get(\"ok\"))\n"
        % (name, payload, name)
    )
    return code


# ---------------------------------------------------------------------------
# Profiles & enablement: which skills the MCP catalog advertises.
#
# File: <module>/profiles_config.json (== #userScripts/MaxMCP/ when
# installed; MAXMCP_PROFILES env override for tests), shape:
#   {"active_profile": str, "profiles": {name: [skill, ...] | None}}
# A None list means "dynamic all" (used only by "All Skills", which can
# neither be overwritten nor deleted).  Live checkbox toggles update
# _ENABLED (and the tools version) immediately but persist only when the
# profile is saved — see max_mcp_ui.
# ---------------------------------------------------------------------------
def profiles_path():
    """Return the profiles JSON path (override via MAXMCP_PROFILES)."""
    override = os.environ.get(PROFILES_ENV)
    if override:
        return os.path.abspath(override)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        here = os.getcwd()
    return os.path.join(here, PROFILES_FILENAME)


def tools_version():
    """Advertised-catalog version; bumped on every enablement change."""
    return int(_TOOLS_VERSION[0])


def on_tools_changed(fn):
    """Register fn(version, enabled_count) for catalog changes.

    Callbacks run synchronously on the mutating thread (the main thread
    for every UI/socket path) and never propagate exceptions.
    """
    if callable(fn) and fn not in _TOOLS_CHANGED_CBS:
        _TOOLS_CHANGED_CBS.append(fn)
    return fn


def _bump_tools_version(reason=""):
    _TOOLS_VERSION[0] = int(_TOOLS_VERSION[0]) + 1
    version = int(_TOOLS_VERSION[0])
    try:
        count = len(_ENABLED)
    except Exception:
        count = 0
    for callback in list(_TOOLS_CHANGED_CBS):
        try:
            callback(version, count)
        except Exception:
            pass
    return version


def _default_shell_skills():
    """Default 'Floor Plan Shell' members: modeling + cleanup categories."""
    return sorted(n for n, spec in SKILL_REGISTRY.items()
                  if spec.category in SHELL_CATEGORIES)


def _default_profiles():
    return {
        PROFILE_ALL: None,
        PROFILE_SHELL: _default_shell_skills(),
        PROFILE_MINIMAL: [],
    }


def _read_profiles_file(path):
    """Return (active, profiles) or (None, error). Never raises."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except IOError:
        return (None, "missing")
    except Exception as exc:
        try:
            backup = path + ".corrupt-%s" % os.getpid()
            try:
                os.replace(path, backup)
            except Exception:
                backup = None
            detail = "corrupt JSON (%s); defaults restored%s" % (
                exc, ("; bad copy kept at %s" % backup) if backup else "")
        except Exception:
            detail = "unreadable (%s); defaults restored" % exc
        return (None, detail)
    try:
        if not isinstance(data, dict):
            return (None, "root must be an object; defaults restored")
        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or PROFILE_ALL not in profiles:
            return (None, "missing profiles table; defaults restored")
        clean = {PROFILE_ALL: None}
        for name, members in profiles.items():
            if name == PROFILE_ALL or not isinstance(name, str) or not name:
                continue
            if not isinstance(members, list):
                continue
            clean[name] = [str(m) for m in members]
        active = data.get("active_profile")
        if active not in clean:
            active = PROFILE_ALL
        return ({"active_profile": active, "profiles": clean}, "")
    except Exception as exc:
        return (None, "invalid schema (%s); defaults restored" % exc)


def _write_profiles_file(path, active, profiles):
    """Atomic write. Returns (ok, error). Never raises."""
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = path + ".tmp-%s" % os.getpid()
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"active_profile": active, "profiles": profiles},
                      handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        return (True, "")
    except Exception as exc:
        return (False, "%s" % exc)


def _ensure_profiles_loaded():
    """Load (or create) the profiles file into _PROFILES. Never raises."""
    global _PROFILES, _PROFILES_ERROR
    if _PROFILES is not None:
        return _PROFILES
    path = profiles_path()
    loaded, err = _read_profiles_file(path)
    if loaded is None:
        if err != "missing":
            _PROFILES_ERROR = err
        loaded = {"active_profile": PROFILE_ALL,
                  "profiles": _default_profiles()}
        ok, werr = _write_profiles_file(path, loaded["active_profile"],
                                        loaded["profiles"])
        if not ok:
            _PROFILES_ERROR = werr
    for name in _default_profiles():
        loaded["profiles"].setdefault(
            name, _default_profiles()[name])
    loaded["profiles"][PROFILE_ALL] = None
    _PROFILES = loaded
    return _PROFILES


def get_all_profiles():
    """Return profile names with 'All Skills' first, rest sorted."""
    state = _ensure_profiles_loaded()
    others = sorted(n for n in state["profiles"] if n != PROFILE_ALL)
    return [PROFILE_ALL] + others


def get_active_profile():
    """Return the active profile name."""
    return _ensure_profiles_loaded()["active_profile"]


def is_skill_enabled(name):
    """Return True when *name* is advertised to MCP clients."""
    return name in _ENABLED


def set_skill_enabled(name, enabled):
    """Toggle one skill live (persists on profile save).

    Bumps the tools version and notifies listeners on real changes.
    Returns (ok, message). Never raises.
    """
    try:
        if name not in SKILL_REGISTRY:
            return (False, "Unknown skill %r." % (name,))
        enabled = bool(enabled)
        before = set(_ENABLED)
        if enabled:
            _ENABLED.add(name)
        else:
            _ENABLED.discard(name)
        if set(_ENABLED) != before:
            _bump_tools_version("toggle %s" % name)
            state = "enabled" if enabled else "disabled"
            return (True, "Skill %r %s (%d active)."
                    % (name, state, len(_ENABLED)))
        return (True, "Skill %r already %s." % (
            name, "enabled" if enabled else "disabled"))
    except Exception as exc:
        return (False, "%s" % exc)


def set_all_enabled(enabled):
    """Enable/disable every registered skill live. Returns (ok, message)."""
    try:
        before = set(_ENABLED)
        if enabled:
            _ENABLED.update(SKILL_REGISTRY)
        else:
            _ENABLED.clear()
        if set(_ENABLED) != before:
            _bump_tools_version("all %s" % ("on" if enabled else "off"))
        return (True, "%d skills %s." % (
            len(_ENABLED), "enabled" if enabled else "disabled"))
    except Exception as exc:
        return (False, "%s" % exc)


def save_profile(profile_name, enabled_skills):
    """Persist *enabled_skills* under *profile_name*. Returns (ok, msg)."""
    try:
        if not profile_name or not isinstance(profile_name, str):
            return (False, "Profile name must be a non-empty string.")
        if profile_name == PROFILE_ALL:
            return (False, "%r is dynamic (always all skills) and cannot "
                           "be overwritten." % PROFILE_ALL)
        if not isinstance(enabled_skills, (list, tuple)):
            return (False, "enabled_skills must be a list of skill names.")
        unknown = [s for s in enabled_skills
                   if s not in SKILL_REGISTRY]
        if unknown:
            return (False, "Unknown skills: %s." % sorted(unknown))
        state = _ensure_profiles_loaded()
        state["profiles"][profile_name] = [str(s) for s in enabled_skills]
        ok, err = _write_profiles_file(profiles_path(),
                                       state["active_profile"],
                                       state["profiles"])
        if not ok:
            return (False, "Write failed: %s" % err)
        return (True, "Profile %r saved (%d skills)."
                % (profile_name, len(state["profiles"][profile_name])))
    except Exception as exc:
        return (False, "%s" % exc)


def load_profile(profile_name):
    """Read one stored profile. Returns (ok, {...} or error string)."""
    try:
        state = _ensure_profiles_loaded()
        if profile_name not in state["profiles"]:
            return (False, "Unknown profile %r. Available: %s"
                    % (profile_name, get_all_profiles()))
        members = state["profiles"][profile_name]
        if members is None:
            members = sorted(SKILL_REGISTRY)
        return (True, {"name": profile_name, "skills": list(members)})
    except Exception as exc:
        return (False, "%s" % exc)


def delete_profile(profile_name):
    """Delete a stored profile. Returns (ok, message). Never raises."""
    try:
        if profile_name == PROFILE_ALL:
            return (False, "%r cannot be deleted." % PROFILE_ALL)
        state = _ensure_profiles_loaded()
        if profile_name not in state["profiles"]:
            return (False, "Unknown profile %r." % (profile_name,))
        del state["profiles"][profile_name]
        if state["active_profile"] == profile_name:
            state["active_profile"] = PROFILE_ALL
            _ENABLED.update(SKILL_REGISTRY)
            _bump_tools_version("delete active %s" % profile_name)
        ok, err = _write_profiles_file(profiles_path(),
                                       state["active_profile"],
                                       state["profiles"])
        if not ok:
            return (False, "Write failed: %s" % err)
        return (True, "Profile %r deleted." % (profile_name,))
    except Exception as exc:
        return (False, "%s" % exc)


def apply_profile(profile_name):
    """Enable exactly *profile_name*'s skills, persist it active.

    Unknown stored names are dropped (reported).  Returns a report dict:
    {"ok", "profile", "enabled", "dropped", "total", "version"}.
    Never raises.
    """
    try:
        state = _ensure_profiles_loaded()
        if profile_name not in state["profiles"]:
            return {"ok": False, "profile": profile_name,
                    "error": "Unknown profile %r. Available: %s"
                             % (profile_name, get_all_profiles()),
                    "enabled": [], "dropped": [],
                    "total": len(_ENABLED),
                    "version": tools_version()}
        members = state["profiles"][profile_name]
        if members is None:
            wanted = set(SKILL_REGISTRY)
            dropped = []
        else:
            wanted = set(m for m in members if m in SKILL_REGISTRY)
            dropped = sorted(set(members) - wanted)
        before = set(_ENABLED)
        _ENABLED.clear()
        _ENABLED.update(wanted)
        version = tools_version()
        if set(_ENABLED) != before:
            version = _bump_tools_version("apply %s" % profile_name)
        state["active_profile"] = profile_name
        ok, err = _write_profiles_file(profiles_path(),
                                       state["active_profile"],
                                       state["profiles"])
        report = {"ok": True, "profile": profile_name,
                  "enabled": sorted(_ENABLED), "dropped": dropped,
                  "total": len(_ENABLED), "version": version}
        if not ok:
            report["persist_warning"] = err
        return report
    except Exception:
        return {"ok": False, "profile": profile_name,
                "error": traceback.format_exc(limit=3),
                "enabled": [], "dropped": [],
                "total": len(_ENABLED), "version": tools_version()}


def _init_profiles():
    """First-run setup: ensure file exists, apply stored active profile."""
    global _PROFILES_ERROR
    state = _ensure_profiles_loaded()
    if _PROFILES_ERROR:
        pass  # surfaced via get_profiles_error(); keep going with defaults
    report = apply_profile(state["active_profile"])
    if not report.get("ok"):
        apply_profile(PROFILE_ALL)
    # Fresh start: the initial enablement is the baseline, not a change.
    _TOOLS_VERSION[0] = 0


def get_profiles_error():
    """Return the last profiles-file problem, or None."""
    _ensure_profiles_loaded()
    return _PROFILES_ERROR


# ---------------------------------------------------------------------------
# Custom skills: <module>/custom_skills/*.py, hot-reloadable from the UI.
#
# A custom file is an ordinary Python script that calls
# ``skills_registry.register_skill`` (usually once).  Files are gated by
# ast.parse before import; per-file syntax/import errors are collected in
# the reload report and never raised.  Built-in skills are never touched:
# removal only drops entries whose function module matches the reloaded
# file.  Remote MCP dispatch reads SKILL_REGISTRY live, so reloaded skills
# are instantly callable remotely as well as from the UI.
# ---------------------------------------------------------------------------
def custom_skills_dir():
    """Return the user-managed skills folder (created on reload)."""
    override = os.environ.get(CUSTOM_SKILLS_ENV)
    if override:
        return os.path.abspath(override)
    try:
        here = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        here = os.getcwd()
    return os.path.join(here, "custom_skills")


def validate_skill_source(source):
    """Syntax-gate skill code without executing it.

    Returns (ok, error).  Parses against the 3ds Max 2022 grammar
    (Python 3.7) when the host interpreter supports feature_version,
    so newer syntax is rejected here instead of exploding in Max.
    Never raises.
    """
    try:
        if isinstance(source, bytes):
            source = source.decode("utf-8", errors="replace")
        if not isinstance(source, str):
            return (False, "source must be text, got %r" % type(source))
        try:
            if sys.version_info >= (3, 8):
                ast.parse(source, feature_version=(3, 7))
            else:
                ast.parse(source)
        except ValueError:
            # Unknown/unsupported feature_version on this interpreter.
            ast.parse(source)
        return (True, "")
    except SyntaxError as exc:
        return (False, "SyntaxError line %s: %s" % (exc.lineno, exc.msg))
    except Exception as exc:
        return (False, "%s" % exc)


def extract_skill_metadata(source):
    """Best-effort parse of the first register_skill(...) call.

    Returns {"name", "title", "category", "description", "params"} with
    sensible defaults, or None when no parseable decorator call exists.
    Used by the UI to prefill the skill editor.  Never raises.
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_reg = (isinstance(func, ast.Name)
                  and func.id == "register_skill") or \
                 (isinstance(func, ast.Attribute)
                  and func.attr == "register_skill")
        if not is_reg:
            continue
        try:
            kw = {}
            for keyword in node.keywords:
                if keyword.arg:
                    kw[keyword.arg] = ast.literal_eval(keyword.value)
        except Exception:
            return None
        params = kw.get("params") or []
        if not isinstance(params, list):
            params = []
        return {"name": kw.get("name"),
                "title": kw.get("title"),
                "category": kw.get("category") or "General",
                "description": kw.get("description") or "",
                "params": params}
    return None


def skill_template(name, display=None, category="General", description="",
                   params=None):
    """Generate a starter <name>.py source for the custom_skills folder.

    The template uses only Python 3.7 syntax, imports pymxs defensively,
    registers via @register_skill, and returns the {"ok": ...} schema both
    inside Max and headlessly.  Never raises (falls back to minimal args).
    """
    try:
        title = display or name
        defs = list(params or [])
        clean = []
        for pdef in defs:
            pname = str(pdef.get("name", "arg"))
            ptype = pdef.get("type", "str")
            if ptype not in VALID_PARAM_TYPES:
                ptype = "str"
            entry = {"name": pname, "type": ptype,
                     "default": pdef.get("default"),
                     "label": pdef.get("label") or
                     pname.replace("_", " ").title()}
            for extra in ("choices", "min", "max", "help"):
                if pdef.get(extra) is not None:
                    entry[extra] = pdef.get(extra)
            clean.append(entry)
        sig = ", ".join("%s=%r" % (p["name"], p["default"]) for p in clean)
        if not sig:
            sig = ""
        lines = []
        lines.append('"""%s (custom MaxMCP skill).' % title)
        lines.append("")
        lines.append("Created from the MaxMCP Skill Manager. Edit the body of")
        lines.append("%s below, then press Reload Skills (no restart needed)." % name)
        lines.append('"""')
        lines.append("")
        lines.append("import traceback")
        lines.append("")
        lines.append("try:")
        lines.append("    from pymxs import runtime as rt")
        lines.append("    _IN_MAX = True")
        lines.append("except Exception:")
        lines.append("    rt = None")
        lines.append("    _IN_MAX = False")
        lines.append("")
        lines.append("from skills_registry import register_skill")
        lines.append("")
        lines.append("")
        lines.append("@register_skill(")
        lines.append("    name=%r," % name)
        lines.append("    title=%r," % title)
        lines.append("    category=%r," % (category or "General"))
        lines.append("    description=%r," % (description or ""))
        lines.append("    params=%r," % (clean,))
        lines.append(")")
        lines.append("def %s(%s):" % (name, sig))
        lines.append('    """%s."""' % title)
        lines.append("    if not _IN_MAX:")
        lines.append('        return {"ok": True, "dry_run_echo": True,')
        lines.append('                "message": "pymxs unavailable '
                     '(outside 3ds Max)."}')
        lines.append("    try:")
        lines.append("        # TODO: implement using %s." % (
            ", ".join(p["name"] for p in clean) or "no parameters"))
        lines.append('        raise NotImplementedError('
                     '"skill body not implemented yet")')
        lines.append("    except Exception:")
        lines.append('        return {"ok": False, "error": '
                     "traceback.format_exc()}")
        return "\n".join(lines) + "\n"
    except Exception as exc:
        return ('"""Skill template generation failed: %s"""\n'
                "from skills_registry import register_skill\n" % exc)


def skill_source_path(name):
    """Return the custom .py path backing *name*, or None for builtins."""
    spec = SKILL_REGISTRY.get(name)
    if spec is None or spec.source == "builtin":
        return None
    return _SKILL_MODULE_FILES.get(spec.source)


def _module_name_for(stem):
    safe = "".join(c if (c.isalnum() or c == "_") else "_"
                   for c in (stem or ""))
    if not safe or safe[0].isdigit():
        safe = "skill_" + safe
    if not safe.isidentifier():
        safe = "skill_custom"
    return "maxmcp_custom_" + safe


def _remove_module_skills(modname):
    """Drop registry entries imported from *modname*. Returns names."""
    removed = []
    for name in list(SKILL_REGISTRY):
        try:
            if getattr(SKILL_REGISTRY[name].func, "__module__",
                       "") == modname:
                del SKILL_REGISTRY[name]
                removed.append(name)
        except Exception:
            pass
    for name in removed:
        _ENABLED.discard(name)
    _SKILL_MODULE_FILES.pop(modname, None)
    try:
        sys.modules.pop(modname, None)
    except Exception:
        pass
    return removed


def reload_skills():
    """(Re)import every .py file in :func:`custom_skills_dir`.

    Creates the folder on first run.  Files starting with "_" are skipped.
    Each file is gated by :func:`validate_skill_source` before import;
    per-file errors are collected in the report and never raised, and
    built-in skills are never removed.  Vanished files drop their skills.

    Returns {"ok", "dir", "loaded", "removed", "errors", "total"}.
    """
    report = {"ok": True, "dir": "", "loaded": [], "removed": [],
              "errors": {}, "total": len(SKILL_REGISTRY)}
    try:
        path = custom_skills_dir()
    except Exception as exc:
        report.update(ok=False, errors={"<dir>": "%s" % exc})
        return report
    report["dir"] = path
    try:
        if not os.path.isdir(path):
            os.makedirs(path)
    except Exception as exc:
        report.update(ok=False, errors={"<mkdir>": "%s" % exc})
        return report
    try:
        files = sorted(f for f in os.listdir(path)
                       if f.endswith(".py") and not f.startswith("_"))
    except Exception as exc:
        report.update(ok=False, errors={"<listdir>": "%s" % exc})
        return report
    wanted = set()
    catalog_before = (sorted(SKILL_REGISTRY), sorted(_ENABLED))
    for fname in files:
        fpath = os.path.join(path, fname)
        modname = _module_name_for(os.path.splitext(fname)[0])
        wanted.add(modname)
        try:
            with open(fpath, "r", encoding="utf-8") as handle:
                source = handle.read()
        except Exception as exc:
            report["errors"][fname] = "cannot read: %s" % exc
            continue
        ok_syntax, syn_err = validate_skill_source(source)
        if not ok_syntax:
            report["errors"][fname] = syn_err
            continue
        before = set(SKILL_REGISTRY)
        _remove_module_skills(modname)
        try:
            loader = importlib.util.spec_from_file_location(modname, fpath)
            if loader is None or loader.loader is None:
                raise ImportError("cannot create loader for %r" % fpath)
            module = importlib.util.module_from_spec(loader)
            sys.modules[modname] = module
            loader.loader.exec_module(module)
        except Exception:
            sys.modules.pop(modname, None)
            report["errors"][fname] = traceback.format_exc(limit=3)
            continue
        _SKILL_MODULE_FILES[modname] = fpath
        added = sorted(set(SKILL_REGISTRY) - before)
        # New skills join the live set only under "All Skills"; any other
        # active profile is explicit, so newcomers stay off until saved in.
        try:
            if get_active_profile() == PROFILE_ALL:
                _ENABLED.update(added)
            else:
                for _n in added:
                    _ENABLED.discard(_n)
        except Exception:
            _ENABLED.update(added)
        if added:
            report["loaded"].extend("%s (%s)" % (n, fname) for n in added)
        else:
            report["loaded"].append("%s (no skills registered)" % fname)
    for modname in [m for m in list(_SKILL_MODULE_FILES)
                    if m not in wanted]:
        report["removed"].extend(_remove_module_skills(modname))
    if (sorted(SKILL_REGISTRY), sorted(_ENABLED)) != catalog_before:
        _bump_tools_version("reload")
    report["total"] = len(SKILL_REGISTRY)
    report["ok"] = not report["errors"]
    return report


# ---------------------------------------------------------------------------
# Built-in demo skills
# ---------------------------------------------------------------------------

@register_skill(
    name="scene_cleaner",
    category="Scene Management",
    description=(
        "Detach isolated vertices on mesh/poly objects, run garbage "
        "collection, and report scene / material counts. "
        "Safe: per-object errors are collected, never raised."
    ),
    params=[
        {"name": "remove_isolated_verts", "type": "bool", "default": True,
         "label": "Remove isolated verts",
         "help": "Delete vertices not referenced by any face."},
        {"name": "clean_unused_materials", "type": "bool", "default": True,
         "label": "Clean unused materials",
         "help": "Flush unassigned scene materials (best effort)."},
        {"name": "dry_run", "type": "bool", "default": False,
         "label": "Dry run (report only)",
         "help": "Count what WOULD change without modifying the scene."},
    ],
)
def scene_cleaner(remove_isolated_verts=True,
                  clean_unused_materials=True, dry_run=False):
    """Skill A: scene hygiene + counts report."""
    if not _IN_MAX:
        return {"ok": True, "dry_run_echo": True,
                "message": "pymxs unavailable (outside 3ds Max); "
                           "no scene touched.",
                "params": {"remove_isolated_verts": bool(remove_isolated_verts),
                           "clean_unused_materials": bool(clean_unused_materials),
                           "dry_run": bool(dry_run)},
                "removed_isolated_verts": 0,
                "cleaned_materials": 0,
                "objects_scanned": 0}
    removed_verts = 0
    scanned = 0
    failed = []
    mat_before = 0
    mat_after = 0
    try:
        with_undo = getattr(rt, "withUndo", None)
        if with_undo is not None:
            rt.withUndo("MCP Scene Cleaner", True)
    except Exception:
        pass
    try:
        try:
            all_objs = list(rt.objects)
        except Exception:
            all_objs = []
        try:
            mat_before = len(list(rt.sceneMaterials))
        except Exception:
            try:
                mat_before = len(list(rt.currentMaterialLibrary or []))
            except Exception:
                mat_before = 0
        if remove_isolated_verts and not dry_run:
            for obj in all_objs:
                try:
                    scanned += 1
                    if not rt.isKindOf(obj, rt.GeometryClass):
                        continue
                    base = rt.getMesh(obj) if hasattr(rt, "getMesh") else None
                    # meshop.deleteIsoVerts returns the number removed.
                    try:
                        if rt.isKindOf(obj, rt.Editable_Poly):
                            n = int(rt.polyop.deleteIsoVerts(obj))
                            removed_verts += max(0, n)
                        else:
                            before = int(rt.meshop.getNumVerts(obj))
                            rt.meshop.deleteIsoVerts(obj)
                            try:
                                rt.update(obj)
                            except Exception:
                                pass
                            after = int(rt.meshop.getNumVerts(obj))
                            removed_verts += max(0, before - after)
                    except Exception as exc:
                        failed.append("%s: %s" % (getattr(obj, "name", "?"), exc))
                except Exception as exc:
                    failed.append("scan: %s" % exc)
        else:
            try:
                scanned = len(all_objs)
            except Exception:
                scanned = 0
        if clean_unused_materials and not dry_run:
            try:
                # Best-effort purge: collect garbage; unassigned materials
                # held only by the medit slots survive, scene refs stay valid.
                rt.gc()
            except Exception as exc:
                failed.append("gc: %s" % exc)
        try:
            mat_after = len(list(rt.sceneMaterials))
        except Exception:
            mat_after = mat_before
    finally:
        try:
            if getattr(rt, "withUndo", None) is not None:
                rt.withUndo("MCP Scene Cleaner", False)
        except Exception:
            pass
    cleaned = max(0, mat_before - mat_after)
    print("scene_cleaner: scanned=%d iso_verts_removed=%d "
          "materials_before=%d after=%d dry_run=%s"
          % (scanned, removed_verts, mat_before, mat_after, dry_run))
    return {"ok": True,
            "objects_scanned": scanned,
            "removed_isolated_verts": int(removed_verts),
            "scene_materials_before": int(mat_before),
            "scene_materials_after": int(mat_after),
            "cleaned_materials": int(cleaned),
            "dry_run": bool(dry_run),
            "failures": failed[:20],
            "failure_count": len(failed)}


@register_skill(
    name="batch_modifier_applier",
    category="Geometry & Modifiers",
    description=(
        "Apply one modifier (TurboSmooth, UVW Map, Smooth, Bend, Taper) "
        "to every selected node with user-defined parameters."
    ),
    params=[
        {"name": "modifier_type", "type": "choice", "default": "TurboSmooth",
         "label": "Modifier",
         "choices": ["TurboSmooth", "UVWMap", "Smooth", "Bend", "Taper"],
         "help": "Which modifier to add on top of each node."},
        {"name": "iterations", "type": "int", "default": 1,
         "label": "Iterations / Amount", "min": 0, "max": 4,
         "help": "TurboSmooth iterations (0-4). For Bend/Taper used as angle/amount."},
        {"name": "uvw_length", "type": "float", "default": 10.0,
         "label": "UVW length", "min": 0.01, "max": 10000.0},
        {"name": "uvw_width", "type": "float", "default": 10.0,
         "label": "UVW width", "min": 0.01, "max": 10000.0},
        {"name": "apply_to_all_if_no_selection", "type": "bool",
         "default": False, "label": "All objects if nothing selected",
         "help": "Fall back to all geometry when selection is empty."},
    ],
)
def batch_modifier_applier(modifier_type="TurboSmooth", iterations=1,
                           uvw_length=10.0, uvw_width=10.0,
                           apply_to_all_if_no_selection=False):
    """Skill B: one modifier across the current selection."""
    if not _IN_MAX:
        return {"ok": True, "dry_run_echo": True,
                "message": "pymxs unavailable (outside 3ds Max).",
                "params": {"modifier_type": modifier_type,
                           "iterations": int(iterations),
                           "uvw_length": float(uvw_length),
                           "uvw_width": float(uvw_width)},
                "applied": 0, "skipped": 0}
    mtype = str(modifier_type)
    applied = 0
    skipped = 0
    details = []

    def _build_modifier():
        if mtype == "TurboSmooth":
            mod = rt.TurboSmooth()
            try:
                mod.iterations = int(iterations)
            except Exception:
                pass
            return mod
        if mtype == "UVWMap":
            mod = rt.UVWMap()
            try:
                mod.length = float(uvw_length)
                mod.width = float(uvw_width)
            except Exception:
                pass
            return mod
        if mtype == "Smooth":
            return rt.Smooth()
        if mtype == "Bend":
            mod = rt.Bend()
            try:
                mod.bendAngle = float(iterations) * 15.0
            except Exception:
                pass
            return mod
        if mtype == "Taper":
            mod = rt.Taper()
            try:
                mod.amount = float(iterations) * 0.2
            except Exception:
                pass
            return mod
        raise ValueError("Unsupported modifier_type: %r" % mtype)

    try:
        targets = list(rt.selection)
    except Exception:
        targets = []
    if not targets and apply_to_all_if_no_selection:
        try:
            targets = [o for o in list(rt.objects)
                       if rt.isKindOf(o, rt.GeometryClass)]
        except Exception:
            targets = []
    if not targets:
        return {"ok": False,
                "error": "Nothing selected. Select nodes first or enable "
                         "'All objects if nothing selected'.",
                "applied": 0, "skipped": 0}
    for node in targets:
        try:
            if not rt.isKindOf(node, rt.GeometryClass):
                skipped += 1
                details.append("%s: skipped (not geometry)" % getattr(node, "name", "?"))
                continue
            rt.addModifier(node, _build_modifier())
            applied += 1
        except Exception as exc:
            skipped += 1
            details.append("%s: %s" % (getattr(node, "name", "?"), exc))
    print("batch_modifier_applier: %s x%d applied=%d skipped=%d"
          % (mtype, len(targets), applied, skipped))
    return {"ok": True, "modifier": mtype,
            "targets": len(targets), "applied": applied,
            "skipped": skipped, "details": details[:20]}


@register_skill(
    name="camera_light_setup",
    category="Materials & Cameras",
    description=(
        "Create a Physical camera aligned to the active viewport plus a "
        "sun + skylight (dome) rig. Renderer-agnostic fallbacks included."
    ),
    params=[
        {"name": "camera_name", "type": "str", "default": "MCP_PhysicalCam",
         "label": "Camera name"},
        {"name": "fov", "type": "float", "default": 45.0,
         "label": "FOV (deg)", "min": 5.0, "max": 120.0},
        {"name": "create_sun", "type": "bool", "default": True,
         "label": "Create sun light"},
        {"name": "sun_angle", "type": "float", "default": 45.0,
         "label": "Sun elevation (deg)", "min": 0.0, "max": 90.0},
        {"name": "create_dome", "type": "bool", "default": True,
         "label": "Create skylight (dome)"},
    ],
)
def camera_light_setup(camera_name="MCP_PhysicalCam", fov=45.0,
                       create_sun=True, sun_angle=45.0, create_dome=True):
    """Skill C: viewport-matched camera + sun/dome lighting."""
    safe_name = (str(camera_name) or "MCP_PhysicalCam").replace('"', "").replace("\\", "")
    if not _IN_MAX:
        return {"ok": True, "dry_run_echo": True,
                "message": "pymxs unavailable (outside 3ds Max).",
                "params": {"camera_name": safe_name, "fov": float(fov),
                           "create_sun": bool(create_sun),
                           "sun_angle": float(sun_angle),
                           "create_dome": bool(create_dome)},
                "created": []}
    created = []
    warnings = []
    try:
        view_tm = None
        try:
            view_tm = rt.viewport.getViewTM()
        except Exception as exc:
            warnings.append("viewport TM unavailable: %s" % exc)
        cam = None
        try:
            cam = rt.PhysicalCamera()
            cam.name = safe_name
        except Exception:
            try:
                cam = rt.TargetCamera()
                cam.name = safe_name
            except Exception as exc:
                return {"ok": False, "error": "Cannot create camera: %s" % exc}
        try:
            if view_tm is not None:
                cam.transform = view_tm
        except Exception as exc:
            warnings.append("camera align failed: %s" % exc)
        try:
            # PhysicalCamera uses film width / focal length; fov maps
            # approximately. TargetCamera takes fov directly.
            if rt.classOf(cam) == rt.PhysicalCamera:
                try:
                    cam.fov = float(fov)
                except Exception:
                    pass
            else:
                cam.fov = float(fov)
        except Exception as exc:
            warnings.append("fov set failed: %s" % exc)
        created.append(str(cam.name))
        if create_sun:
            try:
                try:
                    sun = rt.Sun()
                except Exception:
                    sun = rt.DirectionalLight()
                sun.name = safe_name + "_Sun"
                try:
                    import math
                    elev = math.radians(float(sun_angle))
                    dist = 500.0
                    sun.pos = rt.Point3(0,
                                        -dist * __import__("math").cos(elev),
                                        dist * __import__("math").sin(elev))
                except Exception:
                    pass
                created.append(str(sun.name))
            except Exception as exc:
                warnings.append("sun failed: %s" % exc)
        if create_dome:
            try:
                try:
                    dome = rt.Skylight()
                except Exception:
                    dome = rt.OmniLight()
                dome.name = safe_name + "_Dome"
                created.append(str(dome.name))
            except Exception as exc:
                warnings.append("dome failed: %s" % exc)
    except Exception:
        return {"ok": False, "error": traceback.format_exc()}
    print("camera_light_setup: created=%s warnings=%d"
          % (created, len(warnings)))
    return {"ok": True, "camera": safe_name, "fov": float(fov),
            "created": created, "warnings": warnings}


# ---------------------------------------------------------------------------
# Custom skills autoload: scan custom_skills/ once at import so the UI and
# the MCP dispatch agree from the start.  Per-file errors are recorded in
# AUTOLOAD_REPORT and never raised; use reload_skills() to hot-reload.
# ---------------------------------------------------------------------------
AUTOLOAD_REPORT = None
try:
    AUTOLOAD_REPORT = reload_skills()
except Exception as exc:  # reload_skills guards itself; last-resort net
    AUTOLOAD_REPORT = {"ok": False, "dir": "", "loaded": [], "removed": [],
                       "errors": {"<autoload>": "%s" % exc},
                       "total": len(SKILL_REGISTRY)}

# Profiles init runs after the skill set is complete (builtins + autoload)
# so defaults and stored lists validate against real skill names.
try:
    _init_profiles()
except Exception as exc:  # _init_profiles guards itself; last-resort net
    _PROFILES_ERROR = "%s" % exc
