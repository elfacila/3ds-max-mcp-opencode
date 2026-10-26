# Troubleshooting

## "Max feels frozen" — find the real cause first

1. `python mcp_server_3dsmax.py --ping` — answers = main thread alive.
2. `Get-Process 3dsmax | Select Id, Responding` — `False` = UI message loop dead.
3. Check the `version` field in any response — confirms which script is loaded.
4. Read `%TEMP%\max_mcp_bridge.log` — binds, pumps, heartbeats, failures.

## Field lessons (each cost a debugging round)

1. **Blocking network reads on the UI thread freeze Max.** First design did accept/recv inside the Timer Tick with a 30 s timeout. Fix: all I/O on a daemon thread, queue handoff.
2. **`registerTimeCallback` is not a wall clock.** It fires only while animation time changes — in a static scene requests queue forever. Removed.
3. **Max 2022 `pymxs` has no `rt.execute`.** No MAXScript evaluation from Python — no rollout panels, no `max zoom` commands. Panel became WinForms; zoom became select + press `Z`.
4. **This Max has no `clr`/pythonnet.** No WinForms at all. Surviving pump: ctypes `Win32 SetTimer`. Surviving zoom: `rt.select()`.
5. **`ctypes.wintypes` on Python 3.7 has no `UINT_PTR`.** Use `c_size_t` in the `TIMERPROC` signature.
6. **Startup must verify a live pump.** An early dispatcher reported success with socket-only and requests queued forever. Now: no pump object, no success.
7. **Re-running the script can't rebind the port.** STOP the old listener first (panel, `F11`, or remote stop) or restart Max.
8. **Parametric objects report no faces.** `getNumFaces`/`polyop` fail on Box/Sphere/etc. Use `snapshotAsMesh` first (non-destructive), fall back after.
9. **Far-from-origin scenes hide small objects.** Building at ~182000 with mm units: a 10-unit box at origin is sub-pixel. Create at visible scale (5000), select it, press `Z`. Test boxes accumulate at origin — clean them periodically.
10. **`pymxs` collections are 0-based.** `obj.modifiers[0]`, not `[1]`.
11. **MAXScript-style `name:value` kwargs are invalid Python.** `rt.Box(length=10.0)` — `=` not `:`.
12. **Remote rescue is possible.** The queue-served `stop_max_listener()` freed a wedged session without killing Max (scene saved remotely first via `saveMaxFile`).

## v1.4.x notes

- **Toolbar hidden?** By design since v1.4.1 — run `MaxMCP_showBar()` in the Listener, or drag the macro onto a CUI toolbar (see [[Install-Guide]]).
- **Panel clones on click?** Fixed in v1.4.3 — reinstall the `.mzp` and close leftover duplicates once.

## Emergency sequence

Scene safe? If unsure, save remotely first. Then: panel STOP → `F11` stop → remote stop → kill `3dsmax.exe` (Task Manager) → reopen → Run Script. Check the log to see which pump won.

Related: [[System-Map]], [[Listener]], [[Install-Guide]].
