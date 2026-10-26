/* ==========================================================================
   MaxMCP_Toolbar.ms — compact floating/docked "MCP" button (OPT-IN).

   OFF BY DEFAULT: this file only DEFINES the rollout and MaxMCP_showBar().
   It never shows anything on its own — not at install, not at startup.
   To show the button bar, run MaxMCP_showBar() in the Listener, or use the
   "MCP Tools" menu / macro instead. To hide it again, run:
       try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
       try (destroyDialog MaxMCP_BarRO) catch ()
   The installer copies this file into (getDir #startupScripts) so the
   opt-in command is available every session, and fileIns it once so
   MaxMCP_showBar() exists immediately without a restart.
   Safe to re-run: defining it again changes nothing visible.
   The button calls the "MCP Tools" > "MaxMCP_Launcher" macroScript, so the
   toggle logic (no duplicate windows) lives in one place: max_mcp_ui.py.

   Every step prints to the Listener: after a reboot, open the Listener and
   look for the "[MaxMCP] toolbar" lines. If they are absent, this file is
   not in Startup Scripts (re-run the installer).
   Manual show at any time: Scripting > Run Script > this file (defines
   only), then run MaxMCP_showBar() in the Listener.

   To place the icon on ANOTHER toolbar instead (e.g. the main toolbar):
   Customize > Customize User Interface > Toolbars > Category "MCP Tools"
   > drag "Max MCP" onto any toolbar. It uses the bundled 16/24px icons.
   A macro button on a real CUI toolbar is the most reboot-proof placement
   Max offers (stored in the .cui file itself).
   ========================================================================== */

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

fn MaxMCP_showBar =
(
    -- Show the opt-in button bar on demand. Returns true on success.
    -- NOT called automatically: run this from the Listener when you want
    -- the bar. Safe to re-run: any previous bar instance is removed first.
    local ok = false
    try
    (
        try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
        try (destroyDialog MaxMCP_BarRO) catch ()
        createDialog MaxMCP_BarRO style:#(#style_toolwindow, #style_sysmenu)
        print "[MaxMCP] toolbar: button dialog created."
        try
        (
            cui.registerDialogBar MaxMCP_BarRO
            cui.dockDialogBar MaxMCP_BarRO #cui_dock_top
            print "[MaxMCP] toolbar: docked (top)."
        )
        catch
        (
            print ("[MaxMCP] toolbar: dock refused (" + (getCurrentException()) + "); floating instead.")
            try (cui.registerDialogBar MaxMCP_BarRO) catch ()
        )
        ok = true
    )
    catch
    (
        print ("[MaxMCP] toolbar FAILED: " + (getCurrentException()))
    )
    ok
)

print "[MaxMCP] toolbar startup script loaded (bar hidden by default)."
print "[MaxMCP] toolbar: run MaxMCP_showBar() to show the MCP button."
