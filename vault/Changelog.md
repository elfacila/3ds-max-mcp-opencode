# Changelog

## Listener (`max_listener.py`)

- **1.6.1-ctypes** — ctypes `SetTimer` pump; fixed `UINT_PTR` → `c_size_t` (missing on Python 3.7); blocking fallback reuses the bound socket; WinForms failures logged.
- **1.6.0-form** — WinForms dialog panel (rollouts impossible without `rt.execute`); startup requires a live pump object.
- **1.5.0-dual** — WinForms + native dual pumps; `bridge_status()` thread diagnostics (`MessageLoop`, managed thread id).
- **1.4.1-diag** — `%TEMP%` log file, pump heartbeat, hardened callback form.
- **1.4.0-native** — native `registerTimeCallback` pump attempt (later removed: static-scene dead).
- **1.3.0-panel** — socket thread + queue + WinForms pump; rollout panel; `LISTENER_VERSION` in responses.
- **1.0** — initial: single-threaded polling server, persistent namespace, `__mcp_result__` convention.

## Server (`mcp_server_3dsmax.py`)

- MCP SDK v1/v2 compatible import; tools registered without rebinding (CLI-safe).
- `get_scene_info` polygon counts via `snapshotAsMesh` (was `null` on parametric objects).
- `create_primitive` kwargs fixed to Python `=` syntax; supports 10 primitive types.

## Project

- Initial commit: bridge code, `opencode.json`, `requirements.txt`, `test_mcp.py`, `.gitignore`.
- Obsidian vault: Home, Architecture, Listener, MCP-Server, Protocol, Configuration, Testing, Troubleshooting, Changelog.
- Verified live: full suite + creative session (torus→poly→smooth, twist tower, spline+sweep) against a production scene.
- DXF workflow: `Drawings/Clean CAD.dxf` committed; ezdxf audit → wall-layer extraction → 197 splines built in Max, verified zero-loss (see [[Testing]]).
