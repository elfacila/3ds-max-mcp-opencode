# Configuration (`opencode.json`)

Register the server as a **local** MCP server. Merge the `mcp` block into your OpenCode config (project-local `opencode.json` is picked up automatically when OpenCode launches in that folder).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "3dsmax": {
        "type": "local",
        "command": ["python", "mcp_server_3dsmax.py"],
        "environment": {
          "MAX_HOST": "127.0.0.1",
          "MAX_PORT": "8080",
          "MAX_TIMEOUT": "30"
        }
      }
    }
  }
}
```

Project-local `opencode.json` (relative command — the repo ships this ready to use). OpenCode merges the global `~/.config/opencode/opencode.jsonc` over it: if a deleted server still shows up, check the global file and `/reload`.

Verify with `opencode mcp list` — `3dsmax` should show connected with `get_scene_info`, `execute_python`, `create_primitive`, `list_available_skills`, `execute_skill`. If port 8080 is busy, change `PORT` in `max_listener.py` and `MAX_PORT` here to match.

Related: [[System-Map]], [[MCP-Server]], [[Install-Guide]].
