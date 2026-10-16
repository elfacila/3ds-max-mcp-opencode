# Bridge Legacy (`max_listener.py` + `mcp_server_3dsmax.py`)

The original OpenCode ↔ 3ds Max bridge this vault's older notes (see `vault/`) document. Still live and still the path for the 3 MCP tools.

- **Listener** (`max_listener.py`, v`1.6.1-ctypes`): in-Max TCP server on `127.0.0.1:8080`; background daemon thread for network, ctypes `SetTimer` pump drains the queue on the main thread; persistent exec namespace; `%TEMP%\max_mcp_bridge.log`. Field lessons: no `rt.execute` in 2022 pymxs, `registerTimeCallback` dead in static scenes, no WinForms/`clr` in this install.
- **Server** (`mcp_server_3dsmax.py`): modern-Python MCP/stdio side (SDK v1/v2 compatible); tools `get_scene_info`, `execute_python`, `create_primitive`; standalone CLI flags `--ping/--scene/--box`.
- **Tests**: `test_mcp.py` end-to-end (ping → Box 10³ at origin → scene confirm).

The [[GUI-Plugin]] listener reuses the same [[Protocol]] envelopes alongside it; the two listeners share port 8080 (run one at a time).

Related: [[System-Map]], [[Protocol]], [[Data-Structures]], [[Dev-Log]].
