# Dev Log

Build history, newest first. Design rationale for the current state in [[System-Map]]; legacy bridge detail in `vault/`.

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
