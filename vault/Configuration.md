# Configuration (`opencode.json`)

Register the server as a **local** MCP server. Merge the `mcp` block into your OpenCode config (project-local `opencode.json` is picked up automatically when OpenCode launches in that folder).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "3dsmax": {
      "type": "local",
      "command": ["python", "<ABSOLUTE-PATH>/mcp_server_3dsmax.py"],
      "enabled": true,
      "environment": {
        "MAX_HOST": "127.0.0.1",
        "MAX_PORT": "8080",
        "MAX_TIMEOUT": "30"
      },
      "timeout": 10000
    }
  }
}
```

Verify with `opencode mcp list` — `3dsmax` should show connected with `get_scene_info`, `execute_python`, `create_primitive`. If port 8080 is busy, change `PORT` in `max_listener.py` and `MAX_PORT` here to match.
