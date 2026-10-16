# GUI Plugin (`max_mcp_ui.py`, v1.1.0)

PySide2 panel for 3ds Max 2022 (Python 3.7). Parents to `qtmax.GetQMaxMainWindow()`; all Qt/pymxs imports guarded so transport + registry stay headless-testable.

## Blocks

- **McpBridgeServer** — daemon accept thread (network only) + main-thread `QTimer` pump. Same envelopes as [[Protocol]]. Entry: host/port inputs (`127.0.0.1`/`8080`), Start/Stop toggle, green/red dot, client counter.
- **MaxMcpWindow** — skill tree (grouped by category, display titles, source-path tooltips) + dynamic param form (`QLineEdit`/`QSpinBox`/`QDoubleSpinBox`/`QCheckBox`/`QComboBox`, incl. `node` type) + read-only console + Clear Log.
- **Manage Skills toolbar** — Add New Skill (`AddSkillDialog`: fields + params table + template generator + code editor, `ast` gate before write), Import Skill (`QFileDialog` + pre-copy syntax gate), Edit Skill (file-backed only, id locked), Open Skills Folder (`os.startfile`), Reload Skills (`_hot_reload()` with added/removed diff + reselect).
- **Entry points** — `show_mcp_ui()` / `toggle_mcp_ui()` (macro calls toggle; never duplicates). Close [X] stops the server via `closeEvent`; hide preserves it.

## Safety rules

- Socket threads never touch widgets or `pymxs` (queued-signal hop for logs/clients).
- `execute_skill` never raises; UI wraps it again and warns on failure.
- Management actions log success/errors to the console, never crash Max.

## Tests

`python max_mcp_ui.py --test` — 5/5 (dispatch, aliases, legacy exec, unknown-skill, malformed). Dialog verified by review (no PySide2 off-Max).

Related: [[System-Map]], [[Skills-Registry]], [[Data-Structures]], [[Installer]], [[Dev-Log]].
