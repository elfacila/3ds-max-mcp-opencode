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
4. A docked **MCP Tools** button bar appears immediately; **Max MCP** button toggles the panel. Also present: main-menu `MCP Tools` entry.

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

## Update / uninstall

- **Update**: drop the newer `.mzp` again — files overwrite, `importlib.reload` applies them live.
- **Uninstall**: run `MaxMCP_uninstall()` in the Listener, or drag `uninstall.ms` into a viewport; restart Max to fully unload the macro.

## If it fails

- Listener shows the exact step (`WARNING` lines, never silent aborts).
- Macro launch failures print the full Python traceback (not just `str(e)`).
- Common past failures and causes: [[Installer]] + older field notes in `vault/Troubleshooting.md`.

Related: [[Installer]], [[GUI-Plugin]], [[Skills-Registry]], [[System-Map]].
