# GUI Plugin (`max_mcp_ui.py`, v1.2.0)

PySide2 panel for 3ds Max 2022 (Python 3.7). Parents to `qtmax.GetQMaxMainWindow()`; all Qt/pymxs imports guarded so transport + registry stay headless-testable.

## Blocks

- **McpBridgeServer** — daemon accept thread (network only) + main-thread `QTimer` pump. Same envelopes as [[Protocol]], plus `tools/list` + `await_change` catalog sync. Entry: host/port inputs (`127.0.0.1`/`8080`), Start/Stop toggle, green/red dot, client counter.
- **MaxMcpWindow** — checkbox skill tree in a draggable `QSplitter` (no width cap; tri-state category headers; `● ON/OFF/RUN` status badges) + dynamic param form (`QLineEdit`/`QSpinBox`/`QDoubleSpinBox`/`QCheckBox`/`QComboBox`, incl. `node` type) + read-only console + Clear Log.
- **Profile bar** — preset combo (instant apply), Save/Overwrite, New (+), Delete (−, All Skills protected), Select/Deselect All. Switches log `[PROFILE] Switched to 'X' (N active skills)`; catalog broadcasts `[MCP] tools/list_changed (vN)`.
- **Manage Skills toolbar** — Add New Skill (`AddSkillDialog`: fields + params table + template generator + code editor, `ast` gate before write), Import Skill (`QFileDialog` + pre-copy syntax gate), Edit Skill (file-backed only, id locked), Open Skills Folder (`os.startfile`), Reload Skills (`_hot_reload()` with added/removed diff + reselect).
- **Entry points** — `show_mcp_ui()` / `toggle_mcp_ui()` (macro calls toggle; never duplicates). Close [X] stops the server via `closeEvent`; hide preserves it.

## Safety rules

- Socket threads never touch widgets or `pymxs` (queued-signal hop for logs/clients/catalog events).
- `execute_skill` never raises; UI wraps it again and warns on failure. Explicit local clicks bypass the profile gate (logged); remote dispatch enforces it.
- Management actions log success/errors to the console, never crash Max.

## Tests

`python max_mcp_ui.py --test` — 15/15 (dispatch, aliases, legacy exec, unknown-skill, malformed, profile apply, enabled-only list, wire refusal + local override, stale + parked watch, All-guards, CRUD round-trip). Checkbox/dialog layout verified by review (no PySide2 off-Max).

Related: [[System-Map]], [[Skills-Registry]], [[Data-Structures]], [[Installer]], [[Dev-Log]].
