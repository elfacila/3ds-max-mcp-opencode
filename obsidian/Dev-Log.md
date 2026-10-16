# Dev Log

Build history, newest first. Design rationale for the current state in [[System-Map]]; legacy bridge detail in `vault/`.

## 2026-10-16 — Diagnose verdict: Program Files elevation (installer v1.2.4)

- **User's diagnose output decoded**: payload ✓ macro ✓ receipt (1.2.3) ✓ dotnet ✓ shell ✓ userScripts writable ✓ — but `startupScripts = C:\Program Files\...` (needs admin) and bar missing. Single broken piece identified, everything else healthy.
- **Fix**: bar installs to system Startup when writable, else per-user `#userScripts/Startup/` (documented second autoload folder, no elevation). Receipt/summary record which path won; uninstall cleans both; diagnose checks both.

- **Audit result**: every file runnable off-Max verified green (15/15 UI, 25 + 18 registry suites, live 8080 ping earlier). Entire installer chain never executed live — prime suspect zone.
- **Doc-confirmed bugs fixed**: `copyFile` FAILS when the target exists → `deleteFile` first (re-runs/updates failed 100% before this); stream copy rewritten on the documented BinStream API (`fopen`/`ReadByte`/`WriteByte`/`FClose` + `gc`); `deleteFile` confirmed file-only (dirs untouched).
- **New**: `MaxMCP_Diagnose.ms` (10th MZP file) — zero-risk pre-install check printing all four `getDir`s, installed-file presence, .NET/shell/probe results, live macro registration. `install_receipt.txt` written + read back on install, cleaned on uninstall; one `doesFileExist` after reboot separates "never written" from "deleted later".

- **Ask**: installer must do it all by itself, no Explorer fallback.
- **Fix**: copy chain is now copyFile → .NET `File.Copy` → hidden shell `copy` (outside Max's process, dodges in-process hooks) → manual MAXScript byte stream (`MaxMCP_streamCopy`, partials deleted). Each success path logged (`copied via shell/stream`); total failure prints the manual steps to the Listener. If all four fail it is definitively an OS denial no script can override.

- **Symptom**: panel ran fine in-session (v1.2.0 tree screenshot) but nothing survived restart — no icon, no files. Since Max auto-loads anything present in userMacros/Startup, absence ⟹ files were never durably written (the earlier all-copies-failed run), possibly combined with elevated-vs-user profile mismatch.
- **Fix**: pre-flight writability probe per destination folder (`MaxMCP_probeDir`: .NET write + MAXScript fallback, probe file cleaned up) with abort naming AV / Controlled Folder Access / elevation mismatch; manual Explorer-copy fallback documented. Reboot check + `MaxMCP_showBar()` recovery from the previous entry still apply once files stick.

- **Symptom**: MCP button gone after Max restart. Root cause chain: the earlier all-copies-failed run left no macro/toolbar files behind, so no session could auto-load anything — and the startup script ran silently, leaving zero trace.
- **Fix**: `MaxMCP_Toolbar.ms` restructured around `MaxMCP_showBar()` (manual recovery from the Listener at any time) with a printed line per step; install proves macro registration via lookup-only `createActionItem` (no UI side effects) and prints a post-flight reboot check (`macro file / startup-bar file / payload / macro live`); summary dialog shows registration + reboot-file status.
- **Most reboot-proof placement**: macro button dragged onto a real CUI toolbar (stored in the `.cui` file itself) — steps in [[Install-Guide]] and the success dialog.

## 2026-10-16 — Profiles + live MCP catalog sync (v1.2.0)

- **Profile system** (`skills_registry.py`): `profiles_config.json` (atomic writes, corrupt → backup + defaults), `save/load/delete/get_all/apply_profile`, All Skills dynamic + protected, Floor Plan Shell (modeling + cleanup) / Minimal defaults. Live `_ENABLED` set; `execute_skill` enforces remotely, UI overrides explicitly. `tools_version` + guarded callbacks.
- **Checkbox tree + profile bar** (`max_mcp_ui.py`): tri-state categories, `● ON/OFF/RUN` badges, instant apply with `[PROFILE]` log lines, Select/Deselect All.
- **Catalog sync**: `tools/list` (enabled-only) + `await_change` long-poll (stale answers at once, live parks until change, released on stop). Self-test 15/15; edge suite 18/18.
- **Fix**: skill tree width cap removed → draggable `QSplitter`, wider default panel (full names visible).

## 2026-10-16 — Installer failure fix + compact button

- **Symptom** (from user's Listener screenshot): every `copyFile` in `install.ms` failed, yet the installer printed "installed" and built a button bar pointing at a never-installed macro ("macro not found" popup); menu setup threw "Unknown system exception".
- **Fix** (`install.ms` → v1.2.0): `MaxMCP_ensureDir` verifies every destination folder; `MaxMCP_copyOne` falls back to .NET `System.IO.File.Copy` so the Listener shows the true OS reason (access-denied / Controlled-Folder block / lock); install **aborts** instead of fake succeeding when payload/macro copies fail; menu reuses a leftover "MCP Tools" menu via `findMenu` (the likely throw source) with an index guard.
- **Button**: docked bar shrunk 124×46 → 60×34, label "Max MCP" → "MCP". Real toolbar integration = Customize UI drag-drop (macro auto-appears under "MCP Tools" with bundled icons); steps added to the success dialog and [[Install-Guide]].

## 2026-10-16 — Custom skills manager + vault + push

## 2026-10-16 — Custom skills manager + vault + push

- **Custom skills subsystem** (`skills_registry.py` → v1.1.0): `custom_skills/` autoload + `reload_skills()` (ast 3.7-gated, per-file errors, built-ins protected), `skill_template()`, metadata extraction, new `node` param type. 25-assert headless suite green.
- **Skill Manager UI** (`max_mcp_ui.py` → v1.1.0): toolbar row (Add/Import/Edit/Folder/Reload), modal `AddSkillDialog`, display titles + source tooltips, hot-reload diff logging. Existing 5-test suite still green.
- **MZP repacked** with synced payload.
- **Git**: committed `20960ab` (12 files, +5321), pushed `master` → `elfacila/3ds-max-mcp-opencode`. `opencode.json` left uncommitted (machine-local temp path).
- **This vault** (`obsidian/`): 10 notes + graph config, all cross-linked.

## 2026-10-16 — mzp.run protocol fix

- Drag-drop failed: `Unrecognized run command: try`. Root cause: `mzp.run` is a **control file**, not MAXScript — only protocol commands allowed. Rewrote to `run "install.ms"` + `drop "install.ms"`; repacked; added a validator to the repack script.

## 2026-10-16 — Macro `\U` crash fix

- `Max MCP` button threw `unicodeescape … truncated \UXXXXXXXX escape`; callstack showed the path kept raw backslashes. Root cause: `@"..."` is not verbatim in Max 2022, so `substituteString` never matched (plus a stale installed copy). Fix: `"\\"` escapes + forward-slash normalization; verified by compiling the simulated `pyCmd` payload.

## 2026-10-16 — Macro hardening + installer packaging

- `MCP_MacroScript.ms`: `sys.modules`-guarded reloads, `traceback.print_exc()`, NBSP-free. `toggle_mcp_ui()` added to the UI. Icons generated (pure-Python BMP writer, pixel-verified). `install.ms`/`uninstall.ms` top-level-`local` compile errors fixed via `( … )` blocks. MZP built and validated.

## 2026-10-16 — GUI plugin + skills framework

- `max_mcp_ui.py` (panel, decoupled `McpBridgeServer`, console) + `skills_registry.py` (decorator registry, 3 demo skills, dual UI/MCP execution). Headless self-test 5/5. Connection ping OK (`1.6.1-ctypes`).

## Prior (Sept 2026) — bridge + DXF walls

- Listener evolution `1.0 → 1.6.1-ctypes` (documented in `vault/Changelog.md`); 3 MCP tools verified live; creative session (torus→poly→smooth, twist tower, spline+sweep).
- DXF workflow: `Clean CAD.dxf` → ezdxf audit → 197 wall splines built in Max, zero-loss verified; walls overlay test caught ~10 deviating footprints (documented, rebuild approved).

Related: [[System-Map]], [[Installer]], [[Install-Guide]], [[GUI-Plugin]], [[Skills-Registry]].
