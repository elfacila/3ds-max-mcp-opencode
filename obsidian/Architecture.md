# Architecture

```
┌─────────────┐  MCP/stdio   ┌──────────────────┐  TCP 127.0.0.1:8080  ┌────────────────┐
│   OpenCode  │ ◄──────────► │ mcp_server_3dsmax │ ◄──────────────────► │ 3ds Max 2022   │
│   (agent)   │  5 tools     │  (Python 3.10+)   │  newline-delim JSON  │ max_listener   │
└─────────────┘              └──────────────────┘                      │ (Python 3.7)   │
                                                                      └────────────────┘
```

## Why two processes?

3ds Max 2022 embeds Python 3.7, which cannot run the modern MCP SDK. So the MCP server lives in modern Python and talks to Max over TCP. One short-lived TCP connection per tool call — no persistent state to go stale.

The canonical in-Max side is now the [[GUI-Plugin]] (`McpBridgeServer`: same envelopes + skill dispatch + catalog sync); `max_listener.py` is the legacy/minimal bridge. Run only one bridge at a time (shared port 8080).

## Threading model (listener)

- A background daemon thread does **all** network I/O (accept, recv, send) and never touches `pymxs`.
- Complete requests wait on a `queue.Queue`.
- The queue is drained by a pump on the main thread: WinForms Timer → Win32 `SetTimer` (ctypes) → blocking loop, first working one wins.
- Scene execution therefore always happens on the main thread, as 3ds Max requires, while network waits never block the UI.

See [[Listener]] for the full evolution and [[Troubleshooting]] for why each earlier design failed.

## Constraints discovered in the field

- 3ds Max 2022 `pymxs` has **no `rt.execute`** — no MAXScript evaluation from Python at all.
- `registerTimeCallback` fires only while animation time changes — useless in a static scene.
- This Max install has **no `clr`/pythonnet module** — no WinForms at all.
- Surviving design: pure-stdlib socket thread + ctypes `SetTimer` pump + WinForms-free operation.

Related: [[System-Map]], [[Listener]], [[MCP-Server]], [[Troubleshooting]].
