# Installer (MZP package)

One-click deploy: `installer/MaxMCP_Plugin.mzp` — 10 files at zip root (incl. `MaxMCP_Diagnose.ms`). Drag into a viewport (or Scripting → Run Script). Full file script in [[Install-Guide]].

## Payload

`mzp.run` · `install.ms` · `uninstall.ms` · `MCP_MacroScript.ms` · `MaxMCP_Toolbar.ms` · `max_mcp_ui.py` · `skills_registry.py` · `MaxMCP_24i.bmp` · `MaxMCP_16i.bmp`

## What install.ms does

1. Copies payload to `#userScripts/MaxMCP/` (overwrite = update; `importlib.reload` makes it live, no restart).
2. Registers macro `MCP Tools > Max MCP` from `#userMacros/MaxMCP_Launcher.ms` (auto-loads every session).
3. Icons → `#userIcons/`, linked via `Icon:#("MaxMCP",1)` in the macro.
4. Button bar → `#startupScripts/MaxMCP_Toolbar.ms` as an OPT-IN helper (defines `MaxMCP_showBar()` only, shows nothing); any bar left by an older install is closed.
5. `MCP Tools` main-menu entry via `menuMan.registerMenuContext` (fixed id `0x4D435031`, sticky) — the default entry point.
6. Native-CUI toolbar probe SKIPPED by design since v1.4.1 (menu + opt-in bar cover entry).
7. `cui.saveConfig()` persists UI state.

## Hard-won fixes (see [[Dev-Log]])

- `mzp.run` is a **control file, not MAXScript** — only `run`/`drop`/`copy`/`move`/… commands. Ours is two lines: `run "install.ms"` + `drop "install.ms"`.
- No `local` at file top level in MAXScript — entry points wrapped in `( ... )` blocks.
- Macro uses `"\\"` escapes (never `@"..."` verbatim — Max 2022 doesn't treat it as verbatim, which caused the `\U` unicodeescape crash) + `substituteString` to forward slashes; reloads guarded by `sys.modules`; full `traceback.print_exc()` on failure.
- `MaxMCP_uninstall()` (or drag `uninstall.ms`) removes bar, menu, and files; macro unloads fully on next restart.

Related: [[System-Map]], [[GUI-Plugin]], [[Install-Guide]], [[Dev-Log]], [[Data-Structures]].
