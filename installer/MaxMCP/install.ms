/* ==========================================================================
   MaxMCP Plugin Installer for 3ds Max 2022  (v1.0.0)
   --------------------------------------------------------------------------
   INSTALL:   drag this file -- or the MaxMCP_Plugin.mzp -- into a viewport.
              The installer runs automatically on fileIn.
   UNINSTALL: run  MaxMCP_uninstall()  in the MAXScript Listener, or drag
              uninstall.ms into a viewport.
   WHAT IT DOES:
     1. Copies max_mcp_ui.py + skills_registry.py (+ this file, so the
        uninstaller stays available) into  (getDir #userScripts)/MaxMCP/ .
        Existing copies are overwritten and Python modules reload live.
     2. Installs the "MCP Tools" > "Max MCP" macroScript into
        (getDir #userMacros)  and registers it for the current session
        (userMacros auto-load on every later startup).
     3. Copies MaxMCP_24i.bmp / MaxMCP_16i.bmp into (getDir #userIcons)
        and links them to the macroScript definition.
     4. Installs MaxMCP_Toolbar.ms into (getDir #startupScripts) and runs
        it now: a docked "MCP Tools" button bar appears immediately and is
        re-registered at every startup (persists across sessions).
     5. Adds a persistent "MCP Tools" menu to the main menu bar via the
        menuMan API (stored in the menu file: sticky across sessions).
     6. Probes for a native CUI toolbar API; whatever the outcome, the
        docked button bar from step 4 is always set up as the visible entry
        point, and cui.saveConfig() persists the UI state.
   ========================================================================== */

global MaxMCP_version = "1.0.0"
global MaxMCP_category = "MCP Tools"
global MaxMCP_macroName = "MaxMCP_Launcher"
global MaxMCP_barTitle = "MCP Tools"
global MaxMCP_menuCtx = 0x4D435031  -- fixed id: menu registration is sticky

-- Source folder = folder holding THIS file. Valid while fileIn executes,
-- both for loose .ms drag-drop and for .mzp temp-extraction runs.
global MaxMCP_srcDir = getFilenamePath (getThisScriptFilename())

global MaxMCP_pyFiles = #("max_mcp_ui.py", "skills_registry.py", "install.ms")
global MaxMCP_iconFiles = #("MaxMCP_24i.bmp", "MaxMCP_16i.bmp")


fn MaxMCP_pluginDir = ((getDir #userScripts) + "/MaxMCP/")


fn MaxMCP_copyOne src dst =
(
    if (not (doesFileExist src)) do
    (
        print ("[MaxMCP] MISSING source file: " + src)
        return false
    )
    local ok = copyFile src dst
    if (not ok) do (print ("[MaxMCP] copy FAILED: " + src + " -> " + dst))
    ok
)


fn MaxMCP_ensureMenu =
(
    -- Remove stale "MCP Tools" submenu(s), then rebuild. Returns true/false.
    -- Uses only documented menuMan methods (menuManager reference).
    try
    (
        local bar = menuMan.getMainMenuBar()
        local n = bar.numItems()
        for i = n - 1 to 0 by -1 do
        (
            local it = bar.getItem i
            local sub = try (it.getSubMenu()) catch (undefined)
            if (sub != undefined) do
            (
                if ((try (sub.getTitle()) catch ("")) == MaxMCP_barTitle) do
                    (bar.removeItem it)
            )
        )
        menuMan.registerMenuContext MaxMCP_menuCtx
        local m = menuMan.createMenu MaxMCP_barTitle
        local act = menuMan.createActionItem MaxMCP_macroName MaxMCP_category
        if (act == undefined) do
        (
            print "[MaxMCP] menu skipped: macro not registered yet."
            menuMan.updateMenuBar()
            return false
        )
        m.addItem act -1
        local subItem = menuMan.createSubMenuItem MaxMCP_barTitle m
        bar.addItem subItem (bar.numItems() - 1)  -- insert before Help
        menuMan.updateMenuBar()
        true
    )
    catch
    (
        print ("[MaxMCP] menu setup failed: " + (getCurrentException()))
        false
    )
)


fn MaxMCP_removeMenu =
(
    try
    (
        local bar = menuMan.getMainMenuBar()
        local n = bar.numItems()
        for i = n - 1 to 0 by -1 do
        (
            local it = bar.getItem i
            local sub = try (it.getSubMenu()) catch (undefined)
            if (sub != undefined) do
            (
                if ((try (sub.getTitle()) catch ("")) == MaxMCP_barTitle) do
                    (bar.removeItem it)
            )
        )
        menuMan.updateMenuBar()
        true
    )
    catch (false)
)


fn MaxMCP_tryNativeToolbar =
(
    -- Best-effort probe for a native CUI toolbar API. The documented CUI
    -- struct (2022) exposes save/show but no toolbar-creation methods, so
    -- this is expected to fail on stock Max -- in which case the docked
    -- button bar (always installed) is the toolbar. Never throws.
    try
    (
        local tb = MaxMCP_barTitle
        local found = false
        for i = 1 to (cui.getToolbarCount()) do
        (
            if ((cui.getToolbarName i) == tb) do (found = true)
        )
        if (not found) do (cui.createToolbar tb)
        cui.addItem tb MaxMCP_macroName MaxMCP_category
        cui.showToolbar tb
        true
    )
    catch
    (
        print "[MaxMCP] native CUI toolbar API unavailable; using docked button bar."
        false
    )
)


fn MaxMCP_install =
(
    print ("[MaxMCP] installing v" + MaxMCP_version + " ...")
    local srcDir = MaxMCP_srcDir
    local dstDir = MaxMCP_pluginDir()
    local macroDir = (getDir #userMacros) + "/"
    local iconsDir = (getDir #userIcons) + "/"
    local startupDir = (getDir #startupScripts) + "/"
    makeDir dstDir all:true

    local ok = true

    -- 1. Python payload.
    for f in MaxMCP_pyFiles do
    (
        if (not (MaxMCP_copyOne (srcDir + f) (dstDir + f))) do (ok = false)
    )

    -- 2. MacroScript into userMacros + live registration for this session.
    if (MaxMCP_copyOne (srcDir + "MCP_MacroScript.ms") (macroDir + "MaxMCP_Launcher.ms")) then
    (
        try (fileIn (macroDir + "MaxMCP_Launcher.ms")) catch
        (
            print "[MaxMCP] WARNING: macro installed but live registration failed; restart Max."
        )
    )
    else (ok = false)

    -- 3. Icons into userIcons (non-fatal if missing).
    for f in MaxMCP_iconFiles do
    (
        if (not (MaxMCP_copyOne (srcDir + f) (iconsDir + f))) do
        (
            print "[MaxMCP] WARNING: continuing without custom icons."
        )
    )

    -- 4. Button bar into startup scripts + show it right now.
    if (MaxMCP_copyOne (srcDir + "MaxMCP_Toolbar.ms") (startupDir + "MaxMCP_Toolbar.ms")) then
    (
        try (fileIn (startupDir + "MaxMCP_Toolbar.ms")) catch
        (
            print "[MaxMCP] WARNING: button bar will appear after restart."
        )
    )
    else (ok = false)

    -- 5. Persistent main-menu entry.
    local menuOK = MaxMCP_ensureMenu()

    -- 6. Native CUI toolbar attempt (best effort; bar from step 4 covers us).
    local nativeOK = MaxMCP_tryNativeToolbar()

    -- 7. Persist UI state (Max also saves on exit).
    try (cui.saveConfig()) catch
    (
        print "[MaxMCP] WARNING: cui.saveConfig() failed; UI state saves on Max exit."
    )

    local menuTxt = (if menuOK then "added" else "SKIPPED (see Listener)")
    local barTxt = (if nativeOK then "native CUI toolbar" else "docked button bar")
    local summary = "MaxMCP v" + MaxMCP_version + " installed.\n\n" + \
        "Python:  " + dstDir + "\n" + \
        "Macro:  MCP Tools  >  Max MCP\n" + \
        "Menu:  " + menuTxt + "\n" + \
        "Toolbar:  " + barTxt + "\n\n" + \
        "Press the Max MCP button to show/hide the panel."
    print ("[MaxMCP] " + summary)
    messageBox summary title:"MaxMCP installed"
    (ok and menuOK)
)


fn MaxMCP_uninstall =
(
    print "[MaxMCP] uninstalling ..."
    -- 1. Button bar down.
    try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
    try (destroyDialog MaxMCP_BarRO) catch ()
    -- 2. Main-menu entry out.
    MaxMCP_removeMenu()
    -- 3. Installed files out (folder itself is left in place, empty).
    local dstDir = MaxMCP_pluginDir()
    for f in MaxMCP_pyFiles do
    (
        try (deleteFile (dstDir + f)) catch ()
    )
    try (deleteFile ((getDir #userMacros) + "/MaxMCP_Launcher.ms")) catch ()
    for f in MaxMCP_iconFiles do
    (
        try (deleteFile ((getDir #userIcons) + "/" + f)) catch ()
    )
    try (deleteFile ((getDir #startupScripts) + "/MaxMCP_Toolbar.ms")) catch ()
    -- 4. Persist. The macro fully unloads on the next Max restart.
    try (cui.saveConfig()) catch ()
    messageBox "MaxMCP removed. Restart 3ds Max to unload the macro." title:"MaxMCP"
    true
)


-- --------------------------------------------------------------------------
-- Drag-and-drop entry point: installation runs automatically on fileIn,
-- unless suppressed (uninstall.ms sets MaxMCP_autoInstall = false first).
-- NOTE: `local` is illegal at file top level in MAXScript, so the entry
-- point lives inside this parenthesized block. The try/catch matters:
-- reading a never-declared global throws.
-- --------------------------------------------------------------------------
(
    local _auto = try (MaxMCP_autoInstall) catch (undefined)
    if (_auto == undefined or _auto) do
    (
        MaxMCP_install()
    )
)
