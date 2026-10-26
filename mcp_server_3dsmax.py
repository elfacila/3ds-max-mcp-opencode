"""
MCP Server for OpenCode <-> 3ds Max 2022 bridge (Python 3.10+).

Transport to OpenCode : stdio (MCP protocol, via FastMCP).
Transport to 3ds Max  : TCP socket to the max_listener.py server
                        (default 127.0.0.1:8080, newline-delimited JSON).

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


def send_to_max(code: str, timeout: float = SOCKET_TIMEOUT) -> dict:
    """Send one Python source string to 3ds Max, return the response dict.

    Response keys: id, status ("ok"/"error"), output, result, error.
    Raises MaxConnectionError on socket failure or Max-side error.
    """
    request = {"id": uuid.uuid4().hex, "code": code}
    payload = (json.dumps(request) + "\n").encode("utf-8")
    try:
        with socket.create_connection((MAX_HOST, MAX_PORT), timeout=timeout) as sock:
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
            "Is max_listener.py running inside 3ds Max 2022 "
            "(Scripting -> Run Script)?" % (MAX_HOST, MAX_PORT, exc)
        ) from exc
    if not data.strip():
        raise MaxConnectionError("Empty response from 3ds Max.")
    try:
        response = json.loads(data.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise MaxConnectionError("Invalid JSON from 3ds Max: %s" % exc) from exc
    if response.get("status") != "ok":
        raise MaxConnectionError(
            "3ds Max execution error:\n%s" % (response.get("error") or "unknown")
        )
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
