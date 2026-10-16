/* ==========================================================================
   MaxMCP_Toolbar.ms — floating/docked "MCP Tools" button bar.
   The installer copies this file into (getDir #startupScripts) so 3ds Max
   runs it at every startup (the bar persists across sessions), and fileIns
   it once immediately so the button appears without a restart.
   Safe to re-run: any previous bar instance is unregistered first.
   The button calls the "MCP Tools" > "MaxMCP_Launcher" macroScript, so the
   toggle logic (no duplicate windows) lives in one place: max_mcp_ui.py.
   ========================================================================== */

-- Tear down a previous instance, if any.
try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
try (destroyDialog MaxMCP_BarRO) catch ()

rollout MaxMCP_BarRO "MCP Tools" width:124 height:46
(
    button btn_launch "Max MCP" width:108 height:28 align:#center \
        tooltip:"MCP Bridge: Server + Skills (show/hide)"
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
