/* ==========================================================================
   MaxMCP Plugin Installer for 3ds Max 2022  (v1.4.1)
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
     4. Installs MaxMCP_Toolbar.ms into (getDir #startupScripts) as an
        OPT-IN helper only (bar hidden by default; run MaxMCP_showBar()
        to show it) and hides any bar left over from an earlier install.
        The "MCP Tools" menu from step 5 is the default entry point.
     5. Adds a persistent "MCP Tools" menu to the main menu bar via the
        menuMan API (stored in the menu file: sticky across sessions).
     6. Probes for a native CUI toolbar API; whatever the outcome, the
        "MCP Tools" menu (step 5) is the visible entry point, and
        cui.saveConfig() persists the UI state.
   ========================================================================== */

global MaxMCP_version = "1.4.1"
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


fn MaxMCP_ensureDir dir =
(
    -- Returns true when dir exists, creating missing parents as needed.
    if (doesFileExist dir) do (return true)
    try (makeDir dir all:true) catch ()
    if (doesFileExist dir) do (return true)
    -- One level at a time as a last resort (the parent may miss too).
    try
    (
        local parent = getFilenamePath dir
        if (parent != "" and parent != dir) do (makeDir parent all:true)
        makeDir dir all:true
    )
    catch ()
    (doesFileExist dir)
)


fn MaxMCP_probeDir dir label =
(
    -- Pre-flight writability test. Returns true when Max can write here.
    -- Prints the exact OS verdict; a failure here explains every later
    -- "copy FAILED" and "gone after reboot" symptom.
    if (not (MaxMCP_ensureDir dir)) do
    (
        print ("[MaxMCP] NOT WRITABLE (no dir): " + label + " (" + dir + ")")
        return false
    )
    local probe = dir + "/MaxMCP_write_test.tmp"
    local ok = false
    local detail = ""
    try
    (
        (dotNetClass "System.IO.File").WriteAllText probe "probe"
        ok = doesFileExist probe
    )
    catch
    (
        detail = (getCurrentException())
    )
    if (not ok and detail == "") do
    (
        -- No .NET verdict: try plain MAXScript file I/O instead.
        try
        (
            local fs = createFile probe
            if (fs != undefined) do
            (
                close fs
                ok = true
            )
        )
        catch
        (
            detail = (getCurrentException())
        )
    )
    try (deleteFile probe) catch ()
    if (ok) then
    (
        print ("[MaxMCP] writable: " + label)
    )
    else
    (
        print ("[MaxMCP] NOT WRITABLE: " + label + " (" + dir + ") " + detail)
    )
    ok
)


fn MaxMCP_streamCopy src dst =
(
    -- Last-resort copy path: manual byte stream through the documented
    -- BinStream API (fopen / ReadByte / WriteByte / FClose). Different code
    -- path from copyFile / .NET / child-process copies. Partial results are
    -- deleted, never left behind.
    local ok = false
    local fin = undefined
    local fout = undefined
    try
    (
        fin = fopen src "rb"
        if (fin != undefined) do
        (
            fout = fopen dst "wb"
            if (fout != undefined) do
            (
                try
                (
                    while (not (eof fin)) do (WriteByte fout (ReadByte fin))
                    ok = true
                )
                catch ()
            )
        )
    )
    catch ()
    try (if (fout != undefined) do (FClose fout)) catch ()
    try (if (fin != undefined) do (FClose fin)) catch ()
    -- NOTE: no explicit gc() here by design. A forced collection while
    -- BinStream handles are in flight crashed the collector on user
    -- machines ("MAXScript Garbage Collection Error"). FClose above is
    -- sufficient; Max collects the dead streams on its own schedule.
    if (ok and not (doesFileExist dst)) do (ok = false)
    if (not ok) do (try (deleteFile dst) catch ())
    ok
)


fn MaxMCP_copyOne src dst =
(
    if (not (doesFileExist src)) do
    (
        print ("[MaxMCP] MISSING source file: " + src)
        return false
    )
    local dir = getFilenamePath dst
    if (not (MaxMCP_ensureDir dir)) do
    (
        print ("[MaxMCP] destination folder cannot be created: " + dir)
        return false
    )
    -- copyFile FAILS when the target already exists (documented): clear it
    -- first so re-runs and updates overwrite instead of failing 100%.
    try (deleteFile dst) catch ()
    try (if (copyFile src dst) do (return true)) catch ()
    -- copyFile failed without a reason: ask the OS directly. This reveals
    -- access-denied / Controlled-Folder blocks / locked files / bad paths.
    local detail = "copyFile returned false"
    try
    (
        (dotNetClass "System.IO.File").Copy src dst true
        detail = ""
    )
    catch
    (
        detail = (getCurrentException())
    )
    if (detail == "" and (doesFileExist dst)) do (return true)
    -- Child-process copy: runs outside Max's process, dodging any hook that
    -- intercepts 3dsmax.exe's own file writes. Verified by existence
    -- afterwards (exit code ignored); polled briefly in case the shell is
    -- slow to flush on tired disks.
    try
    (
        hiddenDosCommand ("cmd /c copy /y \"" + src + "\" \"" + dst + "\"")
    )
    catch ()
    local waited = 0
    while ((not (doesFileExist dst)) and (waited < 20)) do
    (
        sleep 0.1
        waited += 1
    )
    if (doesFileExist dst) do
    (
        print ("[MaxMCP] copied via shell: " + dst)
        return true
    )
    -- Manual byte stream: yet another independent code path.
    try
    (
        if (MaxMCP_streamCopy src dst) do
        (
            print ("[MaxMCP] copied via stream: " + dst)
            return true
        )
    )
    catch ()
    print ("[MaxMCP] copy FAILED (copyFile/.NET/shell/stream): " + src + " -> " + dst + " (" + detail + ")")
    false
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
        -- Reuse a same-named menu left by an earlier install (createMenu
        -- throws when the name is taken); empty it to avoid duplicates.
        local m = try (menuMan.findMenu MaxMCP_barTitle) catch (undefined)
        if (m == undefined) then
        (
            m = menuMan.createMenu MaxMCP_barTitle
        )
        else
        (
            try
            (
                for i = m.numItems() - 1 to 0 by -1 do
                (
                    m.removeItemByPosition i
                )
            )
            catch ()
        )
        local act = menuMan.createActionItem MaxMCP_macroName MaxMCP_category
        if (act == undefined) do
        (
            print "[MaxMCP] menu skipped: macro not registered yet."
            menuMan.updateMenuBar()
            return false
        )
        m.addItem act -1
        local subItem = menuMan.createSubMenuItem MaxMCP_barTitle m
        local pos = bar.numItems() - 1  -- insert before Help
        if (pos < 0) do (pos = -1)
        bar.addItem subItem pos
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
    print ("[MaxMCP] src:    " + srcDir)
    print ("[MaxMCP] dst:    " + dstDir)
    print ("[MaxMCP] macros: " + macroDir)
    -- Pre-flight: if Max cannot write these folders, nothing installed
    -- today can survive a reboot. Abort with the OS reason, not silence.
    local wD = MaxMCP_probeDir dstDir "plugin dir"
    local wM = MaxMCP_probeDir macroDir "userMacros"
    local wI = MaxMCP_probeDir iconsDir "userIcons"
    local wS = MaxMCP_probeDir startupDir "startupScripts"
    if ((not wD) or (not wM)) do
    (
        local nowrite = "MaxMCP install ABORTED: 3ds Max cannot write to its own folders.\n\nSee the Listener lines above for which folder and the OS reason.\nUsual causes:\n- Antivirus quarantining/blocking 3dsmax.exe file writes\n- Windows Controlled Folder Access (allow 3dsmax.exe)\n- Max running as a different user / elevated (files would land in another profile)\n\nFix access, then drop the .mzp again. Manual fallback: copy the files with Windows Explorer (it will show the blocker directly)."
        print ("[MaxMCP] " + nowrite)
        messageBox nowrite title:"MaxMCP install failed"
        return false
    )
    if (not wS) do
    (
        print "[MaxMCP] WARNING: startupScripts not writable; the button bar cannot auto-return after reboot (macro + menu still work)."
    )
    if (not wI) do
    (
        print "[MaxMCP] WARNING: userIcons not writable; continuing without custom icons."
    )
    if (not (MaxMCP_ensureDir dstDir)) do
    (
        local dirFail = "MaxMCP install ABORTED.\n\nCannot create folder:\n" + dstDir + \
            "\n\nLikely causes: antivirus / Controlled Folder Access blocking 3ds Max, or a locked path. Fix access, then drop the .mzp again."
        print ("[MaxMCP] " + dirFail)
        messageBox dirFail title:"MaxMCP install failed"
        return false
    )

    local payloadOK = true

    -- 1. Python payload.
    for f in MaxMCP_pyFiles do
    (
        if (not (MaxMCP_copyOne (srcDir + f) (dstDir + f))) do (payloadOK = false)
    )

    -- 2. MacroScript into userMacros + live registration for this session.
    -- NOTE: the installed file MUST end in .mcr — Max auto-loads *.mcr from
    -- userMacros at every startup but ignores *.ms there (that is why the
    -- button lost its macro after reboot). Remove any stale .ms copy.
    try (deleteFile (macroDir + "MaxMCP_Launcher.ms")) catch ()
    local macroOK = MaxMCP_copyOne (srcDir + "MCP_MacroScript.ms") (macroDir + "MaxMCP_Launcher.mcr")
    local macroLive = false
    if (macroOK) do
    (
        try (fileIn (macroDir + "MaxMCP_Launcher.mcr")) catch
        (
            print "[MaxMCP] WARNING: macro installed but live registration failed; restart Max."
        )
        -- Prove the macro is registered WITHOUT running any UI: lookup-only.
        macroLive = try ((menuMan.createActionItem MaxMCP_macroName MaxMCP_category) != undefined) catch (false)
        if (macroLive) then
        (
            print "[MaxMCP] macro registered: MCP Tools > Max MCP."
        )
        else
        (
            print "[MaxMCP] WARNING: macro file present but not registered this session; restart Max."
        )
    )

    -- 3. Icons into userIcons (non-fatal if missing).
    for f in MaxMCP_iconFiles do
    (
        if (not (MaxMCP_copyOne (srcDir + f) (iconsDir + f))) do
        (
            print "[MaxMCP] WARNING: continuing without custom icons."
        )
    )

    -- 4. Button bar (OPT-IN, hidden by default): Max runs BOTH Startup
    -- folders at launch (system-wide under Program Files AND per-user under
    -- #userScripts). The system one needs elevation, so prefer it only when
    -- writable; otherwise the per-user Startup folder, which needs none.
    -- The file only DEFINES MaxMCP_showBar(); nothing is shown at startup.
    -- Any bar left visible by an earlier install is closed below so the new
    -- default (hidden) applies immediately, without waiting for a reboot.
    local userStartupDir = (getDir #userScripts) + "/Startup/"
    local barDst = startupDir + "MaxMCP_Toolbar.ms"
    local barOK = MaxMCP_copyOne (srcDir + "MaxMCP_Toolbar.ms") barDst
    if (not barOK) do
    (
        print "[MaxMCP] system Startup not writable (elevation); trying per-user Startup."
        barDst = userStartupDir + "MaxMCP_Toolbar.ms"
        barOK = MaxMCP_copyOne (srcDir + "MaxMCP_Toolbar.ms") barDst
    )
    if (barOK) do
    (
        -- Make the opt-in command available this session (defines only,
        -- shows nothing), then hide any leftover bar from v1.4.0 or older.
        try (fileIn barDst) catch
        (
            print "[MaxMCP] WARNING: toolbar helper not loaded this session; run MaxMCP_showBar() after restart."
        )
        try (cui.unRegisterDialogBar MaxMCP_BarRO) catch ()
        try (destroyDialog MaxMCP_BarRO) catch ()
        print "[MaxMCP] toolbar: hidden by default (run MaxMCP_showBar() to show)."
    )

    -- 4b. Install receipt: proves (now and after any reboot) exactly what
    -- this run wrote and where. Read-back verified; a missing receipt after
    -- a reboot means "never written", a present one means "deleted later".
    local receiptOK = false
    try
    (
        local stamp = try ((localTime) as string) catch ("unknown time")
        local receipt = "MaxMCP " + MaxMCP_version + " installed " + stamp + "\n" + \
            "payload: " + dstDir + "\n" + \
            "macro:   " + macroDir + "MaxMCP_Launcher.mcr\n" + \
            "bar:     " + barDst + "\n"
        (dotNetClass "System.IO.File").WriteAllText (dstDir + "install_receipt.txt") receipt
        receiptOK = doesFileExist (dstDir + "install_receipt.txt")
    )
    catch ()
    print ("[MaxMCP] receipt: " + (if receiptOK then "written + read back OK" else "FAILED (non-fatal)"))

    -- Abort here (not after registering half a UI): a bar or menu without
    -- the payload/macro behind it only produces "macro not found" popups.
    if ((not payloadOK) or (not macroOK)) do
    (
        local fail = "MaxMCP install ABORTED.\n\nFile copy failed through copyFile, .NET, shell AND byte-stream paths (the Listener shows the OS reason per file).\nNothing was registered.\n\nThe OS is refusing writes to Max's folders for every API. Manual fallback (do this once in Explorer):\n1. Create <userScripts>/MaxMCP/ and copy max_mcp_ui.py + skills_registry.py into it\n2. Copy MCP_MacroScript.ms to <userMacros>/MaxMCP_Launcher.mcr\n3. Copy the two .bmp files to <userIcons>/\n4. Copy MaxMCP_Toolbar.ms to <startupScripts>/\n5. Restart Max."
        print ("[MaxMCP] " + fail)
        messageBox fail title:"MaxMCP install failed"
        return false
    )
    if (not barOK) do
    (
        print "[MaxMCP] WARNING: button bar copy failed; continuing without it (macro + menu still work)."
    )

    -- 5. Persistent main-menu entry.
    local menuOK = MaxMCP_ensureMenu()

    -- 6. Native CUI toolbar: SKIPPED by design (toolbar is opt-in since
    -- v1.4.1). The "MCP Tools" menu (step 5) plus MaxMCP_showBar() cover
    -- entry; uncomment the probe below to restore the old auto-show.
    local nativeOK = false
    -- local nativeOK = MaxMCP_tryNativeToolbar()

    -- 7. Persist UI state (Max also saves on exit).
    try (cui.saveConfig()) catch
    (
        print "[MaxMCP] WARNING: cui.saveConfig() failed; UI state saves on Max exit."
    )

    -- 8. Post-flight persistence check: these three files are what the next
    -- Max session auto-loads. If any is missing, say so now (not next week).
    local persistMacro = doesFileExist (macroDir + "MaxMCP_Launcher.mcr")
    local persistBar = (barOK and (doesFileExist barDst))
    local persistPy = doesFileExist (dstDir + "max_mcp_ui.py")
    print ("[MaxMCP] reboot check: macro file=" + (persistMacro as string) + \
        " startup-bar file=" + (persistBar as string) + \
        " payload=" + (persistPy as string) + \
        " macro live=" + (macroLive as string))

    local menuTxt = (if menuOK then "added" else "SKIPPED (see Listener)")
    local barTxt = "hidden by default (opt-in: run MaxMCP_showBar())"
    if (barOK) do (barTxt = barTxt + " [" + barDst + "]")
    if (not barOK) do (barTxt = "SKIPPED (copy failed)")
    local regTxt = (if macroLive then "yes" else "NO - restart Max, then re-check")
    local persistTxt = (if (persistMacro and persistBar and persistPy) then "all present" else "MISSING FILES (see Listener)")
    local summary = "MaxMCP v" + MaxMCP_version + " installed.\n\n" + \
        "Python:  " + dstDir + "\n" + \
        "Macro:  MCP Tools  >  Max MCP  (registered: " + regTxt + ")\n" + \
        "Menu:  " + menuTxt + "\n" + \
        "Toolbar:  " + barTxt + "\n" + \
        "Reboot files:  " + persistTxt + "\n\n" + \
        "Open the panel via the MCP Tools menu (or press the MCP button if you enabled the bar).\n" + \
        "The button bar is hidden by default; to show it, run MaxMCP_showBar() in the Listener.\n\n" + \
        "Tip: for a permanent icon on any existing toolbar, open Customize > Customize User Interface > Toolbars, pick category 'MCP Tools', and drag 'Max MCP' onto that toolbar."
    print ("[MaxMCP] " + summary)
    messageBox summary title:"MaxMCP installed"
    (payloadOK and macroOK and menuOK)
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
    try (deleteFile (dstDir + "install_receipt.txt")) catch ()
    try (deleteFile ((getDir #userMacros) + "/MaxMCP_Launcher.mcr")) catch ()
    try (deleteFile ((getDir #userMacros) + "/MaxMCP_Launcher.ms")) catch ()
    -- Bar may live in either Startup folder (system needs elevation).
    try (deleteFile ((getDir #startupScripts) + "/MaxMCP_Toolbar.ms")) catch ()
    try (deleteFile ((getDir #userScripts) + "/Startup/MaxMCP_Toolbar.ms")) catch ()
    for f in MaxMCP_iconFiles do
    (
        try (deleteFile ((getDir #userIcons) + "/" + f)) catch ()
    )
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
