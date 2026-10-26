# Listener (`max_listener.py`)

Runs **inside 3ds Max 2022** via Scripting → Run Script. Python 3.7 compatible, stdlib only, no pip packages.

- Listens on `127.0.0.1:8080` (see [[Protocol]]).
- Executes received code with `exec` in a **persistent namespace** (`rt` pre-imported, variables survive across calls).
- Structured results via the `__mcp_result__` convention; stdout/stderr captured; tracebacks returned on error.
- Reports `LISTENER_VERSION` (`1.4.3`) in every response so clients can verify which script is loaded. Legacy bridge — the canonical in-Max transport is now the [[GUI-Plugin]].
- Startup succeeds **only if a pump is actually active** — "already running" never masks a missing pump.
- Independent file log at `%TEMP%\max_mcp_bridge.log` (socket binds, pump heartbeats, failures) — readable even when Max or the socket is wedged.
- `stop_max_listener()` shuts everything down (socket, timers, queue drain).

## Controls

- Floating WinForms panel with STOP / START (only when WinForms exists; absent in this Max — use the Listener instead).
- MAXScript Listener (`F11`): `python.Execute "stop_max_listener()"`.
- Remote stop works through the bridge itself (queue-served).

## Version history

See [[Changelog]].

Related: [[System-Map]], [[Protocol]], [[Changelog]], [[Troubleshooting]].
