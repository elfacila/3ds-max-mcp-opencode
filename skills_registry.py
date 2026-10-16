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

SKILLS_VERSION = "1.1.0"

# name -> SkillSpec
SKILL_REGISTRY = {}

# Custom (user-managed) skills. Directory override for tests/embedders:
#   MAXMCP_CUSTOM_SKILLS=/path/to/dir
CUSTOM_SKILLS_ENV = "MAXMCP_CUSTOM_SKILLS"
# custom module name -> loaded .py path (builtin skills are not tracked here)
_SKILL_MODULE_FILES = {}


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


def list_skills():
    """Return skill metadata dicts sorted by (category, name)."""
    return [SKILL_REGISTRY[k].to_dict()
            for k in sorted(SKILL_REGISTRY,
                            key=lambda n: (SKILL_REGISTRY[n].category,
                                           SKILL_REGISTRY[n].name))]


def skills_by_category():
    """Return {category: [skill_dict, ...]} with skills sorted by name."""
    grouped = {}
    for skill in list_skills():
        grouped.setdefault(skill["category"], []).append(skill)
    return grouped


def get_skill(name):
    """Return the SkillSpec for *name* (or None)."""
    return SKILL_REGISTRY.get(name)


def execute_skill(name, params=None):
    """Run a skill by name with a params dict. Dual entry point.

    Called directly from the UI thread AND from MCP-dispatched code
    running in the main 3ds Max thread.  Always returns a JSON-safe dict.
    Never raises: failures are returned as {"ok": False, "error": ...}.
    """
    try:
        spec = SKILL_REGISTRY.get(name)
        if spec is None:
            return {"ok": False,
                    "skill": name,
                    "error": "Unknown skill %r. Available: %s"
                             % (name, sorted(SKILL_REGISTRY))}
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
        if added:
            report["loaded"].extend("%s (%s)" % (n, fname) for n in added)
        else:
            report["loaded"].append("%s (no skills registered)" % fname)
    for modname in [m for m in list(_SKILL_MODULE_FILES)
                    if m not in wanted]:
        report["removed"].extend(_remove_module_skills(modname))
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
