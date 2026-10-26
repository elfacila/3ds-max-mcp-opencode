# Protocol (TCP wire format)

One JSON object per line (UTF-8), one request → one response, short-lived connection per call. Spoken by [[Bridge-Legacy]] and [[GUI-Plugin]] alike.

## Requests

| Form           | Fields                                                | Notes                                                                                                   |
| -------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Legacy code    | `{"id", "code"}`                                      | Python exec'd in persistent namespace (`rt` pre-imported); `__mcp_result__` returns data                |
| Skill dispatch | `{"id", "skill"⋁"tool", "params"⋁"arguments"⋁"args"}` | served by live `SKILL_REGISTRY` lookup; disabled skills refused → [[Skills-Registry]]                   |
| Catalog list   | `{"id", "tools": "list"}`                             | **enabled-only** skill catalog + version + active profile                                               |
| Catalog watch  | `{"id", "tools": "await_change", "version": N}`       | stale N answers at once; live N parks until the next catalog change (bounded long-poll, re-watch after) |

## Responses

`{"id", "status": "ok"|"error", "output", "result", "error", "version"}` — version `1.4.1` (listener legacy + UI unified) identifies the running side. Catalog answers additionally carry `tools_version` and `update: "changed"` on watch wake-ups.

## Dispatch paths (to [[System-Map]])

1. OpenCode MCP tool → `mcp_server_3dsmax.py` → TCP `{"code"}` → Max executes.
2. OpenCode skill call → `McpBridgeServer` → `execute_skill()` → [[Skills-Registry]].
3. UI button → `execute_skill()` directly (main thread, no socket).

Timeouts: 30 s client read, 120 s main-thread exec wait (timeout returns a structured error, never a hang).

Related: [[System-Map]], [[Data-Structures]], [[GUI-Plugin]], [[Bridge-Legacy]].
