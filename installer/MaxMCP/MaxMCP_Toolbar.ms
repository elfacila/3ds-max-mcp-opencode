/* ==========================================================================
   MaxMCP_Toolbar.ms — compact floating/docked "MCP" button.
   The installer copies this file into (getDir #startupScripts) so 3ds Max
   runs it at every startup (the button persists across sessions), and
   fileIns it once immediately so it appears without a restart.
   Safe to re-run: any previous bar instance is unregistered first.
   The button calls the "MCP Tools" > "MaxMCP_Launcher" macroScript, so the
   toggle logic (no duplicate windows) lives in one place: max_mcp_ui.py.

   To place the icon on ANOTHER toolbar instead (e.g. the main toolbar):
   Customize > Customize User Interface > Toolbars > Category "MCP Tools"
   > drag "Max MCP" onto any toolbar. It uses the bundled 16/24px icons.
   ========================================================================== */

-- Tear down a previous instance, if any.
try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
try (destroyDialog MaxMCP_BarRO) catch ()

rollout MaxMCP_BarRO "MCP" width:60 height:34
(
    button btn_launch "MCP" width:48 height:22 align:#center \
        tooltip:"MaxMCP Server + Skills (show/hide). Tip: Customize > Customize User Interface > Toolbars > MCP Tools > drag Max MCP onto any toolbar."
    on btn_launch pressed do
    (
        -- Macro was registered from (getDir #userMacros); resolve at click
        -- time so load order between startup scripts never matters.
        try
            (macros.run "MCP Tools" "MaxMCP_Launcher")
        catch
            (messageBox "MaxMCP macro not found. Re-run install.ms." title:"Max MCP")
    )
)

createDialog MaxMCP_BarRO style:#(#style_toolwindow, #style_sysmenu)

-- Dock it like a toolbar; fall back to floating if docking is refused.
try
(
    cui.registerDialogBar MaxMCP_BarRO
    cui.dockDialogBar MaxMCP_BarRO #cui_dock_top
)
catch
(
    try (cui.registerDialogBar MaxMCP_BarRO) catch ()
)
