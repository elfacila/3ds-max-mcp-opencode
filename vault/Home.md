# mcp-for-3dsmax

Bridge between **OpenCode** (MCP client) and **3ds Max 2022** (built-in Python 3.7 / pymxs) via a local TCP socket. Modern Python (3.10+) runs the MCP server; 3ds Max runs a listener script.

Open this folder as a vault in Obsidian to browse with wiki-links and graph view.

## Notes

- [[Architecture]] — how the pieces fit together
- [[Listener]] — `max_listener.py`, the 3ds Max side
- [[MCP-Server]] — `mcp_server_3dsmax.py`, the OpenCode side
- [[Protocol]] — TCP wire format between the two
- [[Configuration]] — `opencode.json` registration
- [[Testing]] — test plan, suites, and live results
- [[Troubleshooting]] — every freeze, dead end, and field lesson
- [[Changelog]] — version history

## Quick start

1. In 3ds Max 2022: Scripting → Run Script → `max_listener.py`.
2. `pip install -r requirements.txt`, then `python test_mcp.py` (expect ALL TESTS PASSED).
3. Register [[Configuration]] in OpenCode and prompt away.

## Status

Working: listener v1.6.1 (`pump=ctypes`), all 3 MCP tools verified live against a production architectural scene.
