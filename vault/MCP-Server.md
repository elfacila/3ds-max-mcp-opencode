# MCP Server (`mcp_server_3dsmax.py`)

Runs on **modern Python (3.10+)**. Requires `pip install -r requirements.txt` (`mcp>=1.0.0`). Compatible with MCP SDK **v1** (`FastMCP`) and **v2** (`MCPServer`) via a fallback import.

## Tools

| Tool | Description |
|---|---|
| `get_scene_info` | Lists all scene objects: name, class, position `[x, y, z]`, polygon count (via `snapshotAsMesh`, with mesh/poly fallbacks; `null` for non-geometry). |
| `execute_python` | Runs arbitrary Python/`pymxs` code in 3ds Max. `rt` is pre-imported; set `__mcp_result__` for structured output. |
| `create_primitive` | Box, Sphere, GeoSphere, Teapot, Cylinder, Cone, Torus, Plane, Tube, Pyramid — dimensions + `[x, y, z]` position. Validated server-side; only literals reach Max. |

## Connection handling

- One short-lived TCP connection per call, 30 s default timeout (`MAX_HOST` / `MAX_PORT` / `MAX_TIMEOUT` env overrides).
- Max-side errors surface as `MaxConnectionError` with the remote traceback.

## Standalone CLI (no OpenCode needed)

- `python mcp_server_3dsmax.py --ping` — socket check.
- `python mcp_server_3dsmax.py --scene` — dump scene info.
- `python mcp_server_3dsmax.py --box` — create Box + dump scene.
- No flags → runs as an MCP stdio server for OpenCode.
