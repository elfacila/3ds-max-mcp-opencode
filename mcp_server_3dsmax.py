"""
MCP Server for OpenCode <-> 3ds Max 2022 bridge (Python 3.10+).

Transport to OpenCode : stdio (MCP protocol, via FastMCP).
Transport to 3ds Max  : TCP socket to the pinned listener instance
                        (endpoint file, else MAX_HOST:MAX_PORT env;
                        every response verified by instance_id token).

MCP Tools:
  * get_scene_info  - list all scene objects: name, type, position, poly count.
  * execute_python  - run arbitrary Python/pymxs code inside 3ds Max.
  * create_primitive - create Box/Sphere/Teapot/Cylinder/Cone/Torus/Plane, etc.
  * list_available_skills - query the live skill catalog inside 3ds Max
    (tools version, active profile, enabled skills with schemas).
  * execute_skill - run one named skill inside 3ds Max with a params dict.
    Honors the active profile: disabled skills are refused over the wire.

Run standalone for debugging:
    python mcp_server_3dsmax.py --ping
    python mcp_server_3dsmax.py --scene
    python mcp_server_3dsmax.py --box

Run as MCP server (what OpenCode launches):
    python mcp_server_3dsmax.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import uuid

# --- MCP SDK import (fail with a clear message if missing) -----------------
# Compatible with both MCP SDK v1 (FastMCP) and v2 (MCPServer, renamed).
try:
    from mcp.server.fastmcp import FastMCP  # MCP SDK v1
except ImportError:
    try:
        from mcp.server.mcpserver import MCPServer as FastMCP  # MCP SDK v2
    except ImportError:  # pragma: no cover
        print(
            "Missing dependency: pip install mcp\n"
            "  pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise

# ---------------------------------------------------------------------------
# Configuration (overridable via environment for opencode.json "environment")
# ---------------------------------------------------------------------------
MAX_HOST: str = os.environ.get("MAX_HOST", "127.0.0.1")
MAX_PORT: int = int(os.environ.get("MAX_PORT", "8080"))
SOCKET_TIMEOUT: float = float(os.environ.get("MAX_TIMEOUT", "30"))

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

mcp = FastMCP("3dsmax")


# ---------------------------------------------------------------------------
# Low-level TCP bridge
# ---------------------------------------------------------------------------
class MaxConnectionError(RuntimeError):
    """Raised when 3ds Max cannot be reached or returns an error."""


# ---------------------------------------------------------------------------
# Instance pinning: several 3ds Max sessions may run at once. The MCP link
# must follow ONLY the Max whose listener was started last, and ignore the
# others. The Max side advertises itself in an endpoint file; every listener
# response carries its instance_id token, verified here on each call.
# Last Start wins: the endpoint file is rewritten by every Start, so the
# most recently started listener is always the one we drive.
# ---------------------------------------------------------------------------
ENDPOINT_ENV = "MAXMCP_ENDPOINT"
INSTANCE_ENV = "MAX_INSTANCE_ID"


def _endpoint_path() -> str:
    """Location of the listener-advertised endpoint (overridable for tests)."""
    override = os.environ.get(ENDPOINT_ENV)
    if override:
        return os.path.abspath(override)
    tmp = os.environ.get("TEMP") or os.path.expanduser("~")
    return os.path.join(tmp, "maxmcp_endpoint.json")


def _read_endpoint() -> dict | None:
    """Return {host, port, instance_id, ...} or None when absent/invalid."""
    try:
        with open(_endpoint_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return None
        host = str(data.get("host") or "")
        port = int(data.get("port") or 0)
        token = data.get("instance_id")
        if not host or not port or not token:
            return None
        return {"host": host, "port": port, "instance_id": str(token),
                "pid": data.get("pid"), "version": data.get("version"),
                "source": data.get("source")}
    except Exception:
        return None


def _pinned_instance() -> str | None:
    """Explicitly pinned token (env), or None for endpoint-driven pinning."""
    pin = os.environ.get(INSTANCE_ENV)
    pin = pin.strip() if pin else ""
    return pin or None


_LAST_TARGET: list = [None]  # last (host, port, token) announced in logs
_PIN_CACHE: dict = {}  # (host, port) -> verified token (pre-flight cache)


def _ensure_pinned(host: str, port: int, expected: str | None,
                   timeout: float) -> None:
    """Refuse BEFORE any scene code runs when the holder is foreign.

    Hello-probes the target and compares against the expected token. The
    result is cached per (host, port, token), so the steady state costs
    nothing: a new probe happens only when the endpoint advertisement
    changes (i.e. Start was pressed somewhere). Raises MaxConnectionError
    only for a proven-foreign holder; silence (dead target) is left to the
    send path, which falls back to the env target.
    """
    if not expected:
        return  # unpinned (env mode, no endpoint): legacy behavior
    if _PIN_CACHE.get((host, port)) == expected:
        return
    ident = probe_instance(host, port, timeout=min(timeout, 10.0))
    if ident is None:
        return  # nobody answered: send path will try, then env-fallback
    if ident["instance_id"] != expected:
        raise MaxConnectionError(
            "3ds Max at %s:%d is a DIFFERENT instance (id %.8s, expected "
            "%.8s). Another 3ds Max is holding this port — no code was "
            "sent. Press Start Server in the 3ds Max you want to drive, "
            "then retry." % (host, port, ident["instance_id"], expected)
        )
    _PIN_CACHE[(host, port)] = expected


def _note_target(host: str, port: int, token: str | None) -> None:
    key = (host, port, token)
    if _LAST_TARGET[0] != key:
        _LAST_TARGET[0] = key
        short = ("%.8s" % token) if token else "unpinned"
        logger.info("Linked to 3ds Max instance %s at %s:%d.",
                    short, host, port)


def _resolve_target() -> tuple[tuple[str, int], str | None, bool]:
    """Return ((host, port), expected_token|None, from_endpoint)."""
    ep = _read_endpoint()
    if ep is not None:
        return ((ep["host"], ep["port"]), ep["instance_id"], True)
    return ((MAX_HOST, MAX_PORT), _pinned_instance(), False)


def _send_raw(host: str, port: int, request: dict,
              timeout: float) -> dict:
    """One newline-delimited JSON round-trip. Raises MaxConnectionError."""
    payload = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(payload)
            data = b""
            while not data.endswith(b"\n"):
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
    except (socket.timeout, ConnectionRefusedError, OSError) as exc:
        raise MaxConnectionError(
            "Cannot reach 3ds Max at %s:%d (%s). "
            "Is a listener running inside 3ds Max 2022 "
            "(panel Start Server, or Scripting -> Run Script on "
            "max_listener.py)?" % (host, port, exc)
        ) from exc
    if not data.strip():
        raise MaxConnectionError("Empty response from 3ds Max.")
    try:
        response = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise MaxConnectionError("Invalid JSON from 3ds Max: %s" % exc) from exc
    return response


def _verify_instance(response: dict, expected: str | None,
                     host: str, port: int) -> None:
    """Refuse responses from a different Max instance. Never returns False."""
    if not expected or not isinstance(response, dict):
        return
    actual = response.get("instance_id")
    if actual is None:
        return  # legacy listener without tokens: allow (backward compat)
    if str(actual) != str(expected):
        _PIN_CACHE.pop((host, port), None)  # force a fresh probe next call
        raise MaxConnectionError(
            "Connected 3ds Max at %s:%d answered as a DIFFERENT instance "
            "(id %.8s, expected %.8s). Another 3ds Max is holding this "
            "port. Press Start Server in the 3ds Max you want to drive, "
            "then retry." % (host, port, actual, expected)
        )


def probe_instance(host: str, port: int,
                   timeout: float = 5.0) -> dict | None:
    """Safe identity probe: never executes scene code. Returns identity or None."""
    try:
        response = _send_raw(
            host, port, {"id": "probe-%s" % uuid.uuid4().hex[:8],
                         "hello": True}, timeout)
    except MaxConnectionError:
        return None
    if not isinstance(response, dict) or response.get("status") != "ok":
        return None
    result = response.get("result") or {}
    token = result.get("instance_id") or response.get("instance_id")
    if not token:
        return None
    return {"instance_id": str(token),
            "version": result.get("version") or response.get("version"),
            "source": result.get("source")}


def send_to_max(code: str, timeout: float = SOCKET_TIMEOUT) -> dict:
    """Send one Python source string to 3ds Max, return the response dict.

    Response keys: id, status ("ok"/"error"), output, result, error.
    Talks ONLY to the pinned instance (endpoint file, else env); a live but
    foreign instance is refused instead of driven. Raises MaxConnectionError
    on socket failure, stale endpoints, or Max-side error.
    """
    request = {"id": uuid.uuid4().hex, "code": code}
    (host, port), expected, from_endpoint = _resolve_target()
    _ensure_pinned(host, port, expected, timeout)
    try:
        response = _send_raw(host, port, request, timeout)
    except MaxConnectionError as exc:
        if from_endpoint:
            # Endpoint may be stale (Max closed/crashed). Fall back to the
            # configured env target once before giving up.
            logger.warning("Endpoint target %s:%d unreachable (%s); "
                           "falling back to %s:%d.",
                           host, port, exc, MAX_HOST, MAX_PORT)
            try:
                response = _send_raw(MAX_HOST, MAX_PORT, request, timeout)
                host, port = MAX_HOST, MAX_PORT
                expected = _pinned_instance()
            except MaxConnectionError as exc2:
                raise MaxConnectionError(
                    "Cannot reach 3ds Max at endpoint %s:%d nor at "
                    "%s:%d (%s). If 3ds Max was closed, delete %s or "
                    "press Start Server again." % (
                        host, port, MAX_HOST, MAX_PORT, exc2,
                        _endpoint_path())
                ) from exc2
        else:
            raise
    if response.get("status") != "ok":
        raise MaxConnectionError(
            "3ds Max execution error:\n%s" % (response.get("error") or "unknown")
        )
    _verify_instance(response, expected, host, port)
    _note_target(host, port, response.get("instance_id") or expected)
    return response


def exec_in_max(code: str, timeout: float = SOCKET_TIMEOUT) -> tuple[str, object]:
    """Execute code in Max; return (captured_output, result_value)."""
    response = send_to_max(code, timeout=timeout)
    return response.get("output", ""), response.get("result")


# ---------------------------------------------------------------------------
# Code builders (these Python snippets run INSIDE 3ds Max's interpreter)
# ---------------------------------------------------------------------------
SCENE_INFO_CODE = r"""
import json
from pymxs import runtime as rt

items = []
for o in rt.objects:
    try:
        name = str(o.name)
    except Exception:
        name = "<unnamed>"
    try:
        obj_type = str(rt.classOf(o))
    except Exception:
        obj_type = "Unknown"
    try:
        p = o.pos
        position = [float(p.x), float(p.y), float(p.z)]
    except Exception:
        position = [0.0, 0.0, 0.0]
    poly_count = None
    # Snapshot mesh: works on parametric primitives (Box, Sphere, ...) too,
    # without modifying the object. Fails for non-geometry -> stays None.
    try:
        _snap = rt.snapshotAsMesh(o)
        if _snap is not None:
            poly_count = int(rt.getNumFaces(_snap))
    except Exception:
        poly_count = None
    # Editable Mesh path
    if poly_count is None:
        try:
            if rt.isKindOf(o, rt.GeometryClass):
                poly_count = int(rt.getNumFaces(o))
        except Exception:
            poly_count = None
    # Editable Poly path
    if poly_count is None:
        try:
            poly_count = int(rt.polyop.getNumFaces(o))
        except Exception:
            poly_count = None
    items.append({
        "name": name,
        "type": obj_type,
        "position": position,
        "polygons": poly_count,
    })

__mcp_result__ = {"count": len(items), "objects": items}
print("scene_info: %d objects" % len(items))
"""

# Query the live skill catalog INSIDE 3ds Max (authoritative: reflects the
# active profile and hot-reloaded custom skills on the Max side, which the
# host process cannot see directly - its custom_skills/ dir differs).
SKILLS_CATALOG_CODE = r"""
import skills_registry
__mcp_result__ = {
    "version": skills_registry.tools_version(),
    "active_profile": skills_registry.get_active_profile(),
    "skills": skills_registry.list_enabled_skills(),
}
print("skills: v%d %d enabled (%s)" % (
    __mcp_result__["version"],
    len(__mcp_result__["skills"]),
    __mcp_result__["active_profile"]))
"""


def build_create_code(
    primitive_type: str,
    name: str,
    width: float,
    length: float,
    height: float,
    radius: float,
    position: list[float],
    segments: int,
) -> str:
    """Build the pymxs snippet that creates one primitive.

    Values are interpolated server-side after validation, so the code that
    reaches 3ds Max contains only literals (no injection surface beyond the
    validated floats/known type names).
    """
    ptype = primitive_type.strip().lower()
    x, y, z = float(position[0]), float(position[1]), float(position[2])
    safe_name = name.replace('"', "").replace("\\", "") or "MCP_Object"

    constructors = {
        "box": "rt.Box(length=%r, width=%r, height=%r)" % (length, width, height),
        "sphere": "rt.Sphere(radius=%r, segments=%r)" % (radius, segments * 8),
        "geosphere": "rt.GeoSphere(radius=%r)" % (radius,),
        "teapot": "rt.Teapot(radius=%r)" % (radius,),
        "cylinder": "rt.Cylinder(radius=%r, height=%r)" % (radius, height),
        "cone": "rt.Cone(radius1=%r, height=%r)" % (radius, height),
        "torus": "rt.Torus(majorRadius=%r, minorRadius=%r)"
        % (radius, radius / 4.0 if radius else 1.0),
        "plane": "rt.Plane(length=%r, width=%r)" % (length, width),
        "tube": "rt.Tube(radius1=%r, radius2=%r, height=%r)"
        % (radius, radius / 2.0 if radius else 1.0, height),
        "pyramid": "rt.Pyramid(width=%r, depth=%r, height=%r)"
        % (width, length, height),
    }
    if ptype not in constructors:
        raise ValueError(
            "Unsupported primitive_type %r. Supported: %s"
            % (primitive_type, sorted(constructors))
        )
    ctor = constructors[ptype]
    # NOTE: generated code runs inside 3ds Max (Python 3.7).
    code = (
        "from pymxs import runtime as rt\n"
        "obj = %s\n"
        "obj.name = \"%s\"\n"
        "obj.pos = rt.Point3(%r, %r, %r)\n"
        "__mcp_result__ = {\"name\": obj.name, \"type\": str(rt.classOf(obj)), "
        "\"position\": [float(obj.pos.x), float(obj.pos.y), float(obj.pos.z)]}\n"
        "print(\"created: %%s (%%s) at %%s\" %% (obj.name, rt.classOf(obj), obj.pos))\n"
        % (ctor, safe_name, x, y, z)
    )
    return code


# ---------------------------------------------------------------------------
# MCP Tools (plain functions, registered below without rebinding so the
# standalone CLI can call them directly on any MCP SDK version)
# ---------------------------------------------------------------------------
def get_scene_info() -> str:
    """List all objects in the 3ds Max scene.

    Returns JSON with each object's name, type (class), transform position
    [x, y, z], and polygon/face count (null for non-geometry like lights).
    """
    try:
        output, result = exec_in_max(SCENE_INFO_CODE)
    except MaxConnectionError as exc:
        return "ERROR: %s" % exc
    body = json.dumps(result, indent=2) if result is not None else output
    return "Scene info (%s):\n%s" % (output.strip(), body)


def execute_python(code: str) -> str:
    """Execute arbitrary Python (pymxs) code inside 3ds Max 2022.

    Args:
        code: Python source to exec in 3ds Max's main thread. The variable
            `rt` (pymxs.runtime) is pre-imported. Assign `__mcp_result__`
            to return structured data; stdout is captured and returned.
    """
    if not code or not code.strip():
        return "ERROR: 'code' must be a non-empty string."
    try:
        output, result = exec_in_max(code)
    except MaxConnectionError as exc:
        return "ERROR: %s" % exc
    parts = []
    if output.strip():
        parts.append("Output:\n%s" % output.strip())
    if result is not None:
        try:
            parts.append("Result:\n%s" % json.dumps(result, indent=2))
        except TypeError:
            parts.append("Result:\n%r" % (result,))
    return "\n".join(parts) if parts else "OK (no output)."


def create_primitive(
    primitive_type: str = "Box",
    name: str = "MCP_Box",
    width: float = 10.0,
    length: float = 10.0,
    height: float = 10.0,
    radius: float = 10.0,
    position: list[float] | None = None,
    segments: int = 1,
) -> str:
    """Create a primitive object in 3ds Max at a given position.

    Args:
        primitive_type: Box, Sphere, GeoSphere, Teapot, Cylinder, Cone,
            Torus, Plane, Tube or Pyramid (case-insensitive).
        name: Name for the new node.
        width: Width (Box/Plane/Pyramid). Ignored by radial primitives.
        length: Length/depth (Box/Plane/Pyramid).
        height: Height (Box/Cylinder/Cone/Tube/Pyramid).
        radius: Radius (Sphere/Teapot/Cylinder/Cone/Torus/Tube/GeoSphere).
        position: [x, y, z] world position. Defaults to [0, 0, 0].
        segments: Base segment multiplier (Sphere resolution).
    """
    pos = list(position) if position is not None else [0.0, 0.0, 0.0]
    if len(pos) != 3:
        return "ERROR: 'position' must be [x, y, z]."
    try:
        pos = [float(v) for v in pos]
    except (TypeError, ValueError):
        return "ERROR: 'position' values must be numbers."
    try:
        code = build_create_code(
            primitive_type, name, float(width), float(length),
            float(height), float(radius), pos, int(segments),
        )
    except ValueError as exc:
        return "ERROR: %s" % exc
    try:
        output, result = exec_in_max(code)
    except MaxConnectionError as exc:
        return "ERROR: %s" % exc
    detail = json.dumps(result) if result is not None else output.strip()
    return "Created %s: %s" % (primitive_type, detail)


def list_available_skills() -> str:
    """List the skills currently advertised by 3ds Max.

    Queries the live registry inside 3ds Max (active profile + enabled
    skills with param schemas). Falls back to the host-side registry
    import when 3ds Max is unreachable, so discovery still works offline.
    """
    try:
        output, result = exec_in_max(SKILLS_CATALOG_CODE)
    except MaxConnectionError:
        try:
            import skills_registry as _sr
            result = {
                "version": _sr.tools_version(),
                "active_profile": _sr.get_active_profile(),
                "skills": _sr.list_enabled_skills(),
            }
            body = json.dumps(result, indent=2)
            return "Skills (offline fallback, 3ds Max unreachable):\n%s" % body
        except Exception as exc:
            return "ERROR: %s" % exc
    body = json.dumps(result, indent=2) if result is not None else output
    return "Skills (%s):\n%s" % (output.strip(), body)


def execute_skill(skill: str, params: dict | None = None) -> str:
    """Run one named skill inside 3ds Max with a params dict.

    Args:
        skill: Skill name, e.g. "my_skill".
        params: Dict of skill params (merged over schema defaults).
    Honors the active profile: disabled skills are refused over the wire.
    """
    name = (skill or "").strip()
    if not name:
        return "ERROR: 'skill' must be a non-empty string."
    args = dict(params) if isinstance(params, dict) else {}
    try:
        import skills_registry as _sr
        code = _sr.build_skill_code(name, args)
    except Exception:
        code = (
            "import skills_registry\n"
            "__mcp_result__ = skills_registry.execute_skill(%r, %s)\n"
            % (name, json.dumps(args, sort_keys=True))
        )
    try:
        output, result = exec_in_max(code)
    except MaxConnectionError as exc:
        return "ERROR: %s" % exc
    if isinstance(result, dict) and not result.get("ok"):
        return "Skill %r FAILED: %s\n%s" % (
            name, result.get("error", "unknown error"),
            json.dumps(result, indent=2))
    parts = []
    if output.strip():
        parts.append("Output:\n%s" % output.strip())
    if result is not None:
        try:
            parts.append("Result:\n%s" % json.dumps(result, indent=2))
        except TypeError:
            parts.append("Result:\n%r" % (result,))
    return "\n".join(parts) if parts else "OK (no output)."


# Register the tools with the MCP server (no rebinding: the names above stay
# plain callables for the standalone CLI on any SDK version).
mcp.tool()(get_scene_info)
mcp.tool()(execute_python)
mcp.tool()(create_primitive)
mcp.tool()(list_available_skills)
mcp.tool()(execute_skill)


# ---------------------------------------------------------------------------
# Standalone CLI (debugging without OpenCode)
# ---------------------------------------------------------------------------
def _cli() -> int:
    parser = argparse.ArgumentParser(description="3ds Max MCP server / tester")
    parser.add_argument("--ping", action="store_true", help="socket check only")
    parser.add_argument("--scene", action="store_true", help="call get_scene_info")
    parser.add_argument("--skills", action="store_true",
                        help="query live skill catalog (offline fallback ok)")
    parser.add_argument(
        "--box", action="store_true",
        help="create Box 10x10x10 at origin, then get_scene_info",
    )
    args = parser.parse_args()

    if args.ping:
        (host, port), expected, from_endpoint = _resolve_target()
        where = "endpoint file %s" % _endpoint_path() if from_endpoint \
            else "env %s:%d" % (MAX_HOST, MAX_PORT)
        print("Target: %s:%d (%s)%s" % (
            host, port, where,
            (" expected instance %.8s" % expected) if expected else ""))
        try:
            output, _ = exec_in_max('print("ping-ok")\n__mcp_result__ = "pong"')
            print("PING OK: %s" % output.strip())
            return 0
        except MaxConnectionError as exc:
            print("PING FAILED: %s" % exc, file=sys.stderr)
            return 1
    if args.scene:
        print(get_scene_info())
        return 0
    if args.skills:
        print(list_available_skills())
        return 0
    if args.box:
        print(create_primitive("Box", "MCP_Box", 10.0, 10.0, 10.0, 10.0,
                               [0.0, 0.0, 0.0], 1))
        print(get_scene_info())
        return 0
    # Default: run as an MCP stdio server for OpenCode.
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
