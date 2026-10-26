# 3ds Max MCP Bridge (3ds Max 2022)

OpenCode <-> 3ds Max 2022 bridge: MCP stdio server on the host, TCP socket to a
main-thread pump inside Max, plus an optional Qt GUI plugin with a skill
framework.

## Components (v1.4.1, single version across all files)

- `mcp_server_3dsmax.py` — MCP stdio server (what OpenCode launches).
  Tools: `get_scene_info`, `execute_python`, `create_primitive`,
  `list_available_skills`, `execute_skill`.
  Debug: `python mcp_server_3dsmax.py --ping|--scene|--skills|--box`
- `max_mcp_ui.py` — canonical in-Max transport + GUI (`McpBridgeServer`,
  `MaxMcpWindow`). Skill dispatch, `tools/list` + `await_change`
  long-poll, profiles, hot-reload. Headless check: `python max_mcp_ui.py --test`
- `max_listener.py` — LEGACY minimal bridge (no GUI, no skills).
  Kept for headless/protocol reference. Run only ONE bridge at a time
  (both default to `127.0.0.1:8080`).
- `skills_registry.py` — decoupled skill framework (no sockets, no Qt).
  Ships with NO built-in skills: the registry starts empty and the UI keeps
  stable category folders (Scene Management, Geometry & Modifiers,
  Materials & Cameras) so manually added skills can be sorted into them.
  Custom skills: drop `.py` files using `@register_skill` into `custom_skills/`,
  then Reload (folder is git-ignored runtime state).
- `test_mcp.py` — end-to-end socket test vs a live Max (ping + Box + scene).
- `installer/MaxMCP/` — Max-side payload (`install.ms`, `max_mcp_ui.py`,
  `skills_registry.py`, toolbar/menu/macro). `MaxMCP_Plugin.mzp` is built from
  these files — keep root copies as source of truth and re-sync before release.
- `profiles_config.example.json` — copy to `profiles_config.json` to
  customize; the live file is git-ignored per-machine state.

## Install

1. `pip install -r requirements.txt` (pins `mcp>=2.0,<3`, tested on 2.2.0).
2. Drag `installer/MaxMCP_Plugin.mzp` (or `installer/MaxMCP/install.ms`)
   into a 3ds Max viewport. Press the MCP button to show the panel,
   Start Server.
3. `opencode.json` points at `mcp_server_3dsmax.py` (relative path, enabled).
   No global MCP config needed.

## Protocol (newline-delimited JSON, one request per connection)

- `{"id", "code"}` — exec Python in Max main thread, `__mcp_result__` returned.
- `{"id", "skill", "params"}` — skill dispatch (`tool`/`arguments`/`args` aliases).
- `{"id", "tools": "list"}` / `{"id", "tools": "await_change", "version"}`.
- Response: `{"id","status":"ok"|"error","output","result","error","version"}`.

## Testing

- `python max_mcp_ui.py --test` — registry + transport + profiles (no Max needed).
- `python test_mcp.py` — needs live Max with a bridge listening on 8080.
- `python mcp_server_3dsmax.py --skills` — live catalog, offline fallback ok.

## Notes

- `*.max`, `profiles_config.json`, `custom_skills/` are git-ignored
  (local machine state). Commit `profiles_config.example.json` instead.
- If OpenCode still shows a deleted server, check the global layer
  `~/.config/opencode/opencode.jsonc` — it merges over project config.
  `/reload` applies config changes without restart.
