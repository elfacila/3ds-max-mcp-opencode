# Testing

## Automated suite (`test_mcp.py`, no MCP dependency)

1. **Connectivity** — ping/pong round-trip against the listener.
2. **Create** — spawns `MCP_TestBox_10` (Box 10×10×10 at origin), asserts name + position from the structured result.
3. **Confirm** — `get_scene_info` must list the box at `[0,0,0]` with polygon count `> 0`.

Run: `python test_mcp.py` → exit 0 + `ALL TESTS PASSED`.

## Regression harnesses (throwaway, `%TEMP%`, not committed)

- `mock_max.py` — stubbed `pymxs` serving the **real** `handle_request_line`; caught the MAXScript-style `name:value` kwargs bug and a `%%` escaping bug before ever touching Max.
- `harness2.py` — stubbed `pymxs` + **real** socket thread and queue pump.
- `slow_test.py` — slowloris-style client holds a connection 3 s while a ping must answer fast (passed at 0.05 s — the old design would wedge up to 30 s).

## Live results (production architectural scene, 13 objects)

- `test_mcp.py`: ALL TESTS PASSED against real 3ds Max 2022.
- MCP CLI: `--ping` / `--box` / `--skills` pass; SDK lists `create_primitive`, `execute_python`, `get_scene_info`, `list_available_skills`, `execute_skill`.
- Creative session: Torus → Editable Poly → Smooth (Auto Smooth on), twisted tower (Box + Twist 180°), S-curve spline + Sweep, rescale/delete/select flows — all executed live.

## DXF import (`Drawings/Clean CAD.dxf`)

- Audit: 1.4 MB, 7292 entities (6631 LINE), 32 layers, no `$INSUNITS` (treated as mm).
- Extracted wall layers `A-WALL`, `A-WALL-1`, `A-WALL-2`, `A-WALL-FNSH-1`, `I-WALL` → 197 LINE segments, bbox ≈ 12×9 m, placed 1:1 near origin.
- Built one `Line` shape per layer (`DXF_<LAYER>`), one spline per segment, corner knots, `updateShape` — 47.6 KB single request.
- Verified back from Max: 197 splines / 394 knots, zero loss.
- Open follow-ups: `A-GLAZ` (4019 segments), `A-DOOR` (549 lines + 67 arcs).

## Manual verification guide

1. 3ds Max 2022 → Scripting → Run Script → `max_listener.py`. Expect `START version=1.4.3 pump=ctypes` in `%TEMP%\max_mcp_bridge.log` (or the [[GUI-Plugin]] panel → Start Server).
2. `python mcp_server_3dsmax.py --ping` → `PING OK`.
3. `python test_mcp.py` → `ALL TESTS PASSED`.
4. `opencode mcp list` shows `3dsmax` connected.

Related: [[System-Map]], [[MCP-Server]], [[Bridge-Legacy]].
