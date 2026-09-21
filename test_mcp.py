"""
End-to-end test for the OpenCode <-> 3ds Max 2022 bridge (no MCP dependency).

Covers the required Test Plan:
  1. Socket connectivity verification against max_listener.py.
  2. create_primitive equivalent: spawn a Box 10x10x10 at (0,0,0).
  3. get_scene_info equivalent: confirm the Box exists.

Prerequisites:
  - 3ds Max 2022 open, max_listener.py started via Scripting -> Run Script
    (listener must print "Listening on 127.0.0.1:8080").
  - Modern Python (3.10+) on this machine:  python test_mcp.py

Exit code 0 = all checks passed. Non-zero = failure (message on stderr).
"""

import json
import socket
import sys
import uuid

HOST = "127.0.0.1"
PORT = 8080
TIMEOUT = 30.0

BOX_NAME = "MCP_TestBox_10"

CREATE_BOX_CODE = (
    "from pymxs import runtime as rt\n"
    "for o in list(rt.objects):\n"
    "    _n = ''\n"
    "    try:\n"
    "        _n = str(o.name)\n"
    "    except Exception:\n"
    "        pass\n"
    "    if _n == '%s':\n"
    "        rt.delete(o)\n"
    "obj = rt.Box(length=10.0, width=10.0, height=10.0)\n"
    "obj.name = '%s'\n"
    "obj.pos = rt.Point3(0.0, 0.0, 0.0)\n"
    "__mcp_result__ = {'name': obj.name, 'type': str(rt.classOf(obj)), "
    "'position': [float(obj.pos.x), float(obj.pos.y), float(obj.pos.z)]}\n"
    "print('created: %%s (%%s) at %%s' %% (obj.name, rt.classOf(obj), obj.pos))\n"
) % (BOX_NAME, BOX_NAME)

SCENE_INFO_CODE = (
    "from pymxs import runtime as rt\n"
    "items = []\n"
    "for o in rt.objects:\n"
    "    try:\n"
    "        _name = str(o.name)\n"
    "    except Exception:\n"
    "        _name = '<unnamed>'\n"
    "    try:\n"
    "        _type = str(rt.classOf(o))\n"
    "    except Exception:\n"
    "        _type = 'Unknown'\n"
    "    try:\n"
    "        _p = o.pos\n"
    "        _pos = [float(_p.x), float(_p.y), float(_p.z)]\n"
    "    except Exception:\n"
    "        _pos = [0.0, 0.0, 0.0]\n"
    "    _polys = None\n"
    "    try:\n"
    "        _snap = rt.snapshotAsMesh(o)\n"
    "        if _snap is not None:\n"
    "            _polys = int(rt.getNumFaces(_snap))\n"
    "    except Exception:\n"
    "        _polys = None\n"
    "    if _polys is None:\n"
    "        try:\n"
    "            if rt.isKindOf(o, rt.GeometryClass):\n"
    "                _polys = int(rt.getNumFaces(o))\n"
    "        except Exception:\n"
    "            _polys = None\n"
    "    if _polys is None:\n"
    "        try:\n"
    "            _polys = int(rt.polyop.getNumFaces(o))\n"
    "        except Exception:\n"
    "            _polys = None\n"
    "    items.append({'name': _name, 'type': _type, "
    "'position': _pos, 'polygons': _polys})\n"
    "__mcp_result__ = {'count': len(items), 'objects': items}\n"
    "print('scene_info: %d objects' % len(items))\n"
)


def send(code):
    """Send one code string to 3ds Max, return the parsed response dict."""
    request = {"id": uuid.uuid4().hex, "code": code}
    payload = (json.dumps(request) + "\n").encode("utf-8")
    with socket.create_connection((HOST, PORT), timeout=TIMEOUT) as sock:
        sock.settimeout(TIMEOUT)
        sock.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    if not data.strip():
        raise RuntimeError("Empty response from 3ds Max.")
    return json.loads(data.decode("utf-8"))


def check(condition, message):
    status = "PASS" if condition else "FAIL"
    print("[%s] %s" % (status, message))
    if not condition:
        raise SystemExit("TEST FAILED: %s" % message)


def main():
    print("== 3ds Max MCP bridge test -> %s:%d ==" % (HOST, PORT))

    # Step 1: socket connectivity (ping).
    try:
        resp = send('print("ping-ok")\n__mcp_result__ = "pong"')
    except (ConnectionRefusedError, socket.timeout, OSError) as exc:
        print("ERROR: cannot reach 3ds Max at %s:%d: %s" % (HOST, PORT, exc),
              file=sys.stderr)
        print("HINT: in 3ds Max 2022 go to Scripting -> Run Script and run "
              "max_listener.py, then retry.", file=sys.stderr)
        raise SystemExit(1)
    check(resp.get("status") == "ok",
          "connectivity: listener answered (output=%r)" % resp.get("output"))
    check(resp.get("result") == "pong", "connectivity: result == 'pong'")

    # Step 2: create Box 10x10x10 at origin.
    resp = send(CREATE_BOX_CODE)
    check(resp.get("status") == "ok",
          "create_primitive: no Max-side error (%s)"
          % (resp.get("output") or "").strip())
    result = resp.get("result") or {}
    check(result.get("name") == BOX_NAME,
          "create_primitive: result name == %r (got %r)"
          % (BOX_NAME, result.get("name")))
    pos = result.get("position")
    check(pos == [0.0, 0.0, 0.0],
          "create_primitive: position == [0,0,0] (got %r)" % (pos,))

    # Step 3: get_scene_info confirms the Box.
    resp = send(SCENE_INFO_CODE)
    check(resp.get("status") == "ok", "get_scene_info: no Max-side error")
    result = resp.get("result") or {}
    names = [o.get("name") for o in result.get("objects", [])]
    check(BOX_NAME in names,
          "get_scene_info: %r present in scene (%d objects)"
          % (BOX_NAME, result.get("count")))
    match = next(o for o in result["objects"] if o.get("name") == BOX_NAME)
    print("Box entry: %s" % json.dumps(match))
    check(match.get("position") == [0.0, 0.0, 0.0],
          "get_scene_info: Box position confirmed at origin")
    check(isinstance(match.get("polygons"), int) and match["polygons"] > 0,
          "get_scene_info: Box polygon count = %r" % match.get("polygons"))

    print("ALL TESTS PASSED.")


if __name__ == "__main__":
    main()
