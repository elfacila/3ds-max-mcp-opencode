# Install Guide

End-to-end deployment of MaxMCP into 3ds Max 2022. For the design behind each step see [[Installer]].

## Prerequisites

- 3ds Max 2022 (built-in Python 3.7 + PySide2 + pymxs).
- The `installer/MaxMCP_Plugin.mzp` from this repo (commit `20960ab+`).
- No pip packages needed inside Max.

## Install (one click)

1. Drag `MaxMCP_Plugin.mzp` into any 3ds Max viewport (or Scripting → Run Script → select it).
2. `mzp.run` fires `run "install.ms"` / `drop "install.ms"` → the installer runs automatically.
3. Confirm the summary dialog: Python dir, macro, menu, toolbar lines.
4. No button bar appears — it is **opt-in since v1.4.1**. Open the panel via the main-menu `MCP Tools` entry (or run `MaxMCP_showBar()` in the Listener for the docked bar).

## Where things land

| What | Path |
|---|---|
| Plugin + skills | `(getDir #userScripts)/MaxMCP/` |
| Launcher macro | `(getDir #userMacros)/MaxMCP_Launcher.ms` |
| Icons | `(getDir #userIcons)/MaxMCP_{16,24}i.bmp` |
| Button bar | `(getDir #startupScripts)/MaxMCP_Toolbar.ms` |
| Your skills | `…/MaxMCP/custom_skills/` (auto-created on first reload) |

## First run

1. Press **Max MCP** → panel opens (parented to the main window).
2. MCP Listener → Start Server (`127.0.0.1:8080`, green dot).
3. Manage skills: **Add New Skill** (template → save → auto-reload), or drop `.py` files into `custom_skills/` and press **Reload Skills**.
4. Verify: `python test_mcp.py` from the repo (expects ALL TESTS PASSED).

## After a reboot

The button bar stays hidden: the Startup script only defines `MaxMCP_showBar()` — open the Listener and confirm the `[MaxMCP] toolbar ... hidden by default` lines. If they are absent, `MaxMCP_Toolbar.ms` is not in Startup Scripts: re-drop the `.mzp`. To show the bar at any time, run `MaxMCP_showBar()` in the Listener. The `MCP Tools` menu and the Customize-dragged icon (below) work with no bar at all.

## Update / uninstall

- **Update**: drop the newer `.mzp` again — files overwrite, `importlib.reload` applies them live.
- **Uninstall**: run `MaxMCP_uninstall()` in the Listener, or drag `uninstall.ms` into a viewport; restart Max to fully unload the macro.

## Putting the icon on another toolbar

The bundled docked bar is deliberately tiny (60×34, "MCP" button) and hidden by default. For a normal-size icon alongside your other tools (the recommended setup):

1. Customize → Customize User Interface → **Toolbars** tab.
2. Category: **MCP Tools** → drag **Max MCP** onto any toolbar (e.g. the main toolbar).
3. It uses the bundled `MaxMCP_16i.bmp` / `MaxMCP_24i.bmp` icons automatically; position persists via `cui.saveConfig()`.

## If it fails

- Listener shows the exact step (`WARNING` lines, never silent aborts).
- Macro launch failures print the full Python traceback (not just `str(e)`).
- Common past failures and causes: [[Installer]] + field notes in [[Troubleshooting]].

Related: [[Installer]], [[GUI-Plugin]], [[Skills-Registry]], [[System-Map]].
