/* ==========================================================================
   MaxMCP_Diagnose.ms — pre-install environment check for 3ds Max 2022.
   Run via Scripting > Run Script (or drag into a viewport). Writes nothing
   permanent: one temp probe file per folder, deleted immediately.
   Paste the ENTIRE Listener output when reporting install problems.
   NOTE: `local` is illegal at file top level, hence the outer block.
   ========================================================================== */
(
    print "========== MaxMCP diagnose =========="
    local dirs = #(#userScripts, #userMacros, #userIcons, #startupScripts)
    for d in dirs do
    (
        try
        (
            print ("dir " + (d as string) + " = " + (getDir d))
        )
        catch
        (
            print ("dir " + (d as string) + " UNREADABLE: " + (getCurrentException()))
        )
    )
    local payload = (getDir #userScripts) + "/MaxMCP/max_mcp_ui.py"
    local macro = (getDir #userMacros) + "/MaxMCP_Launcher.mcr"
    local macroStale = (getDir #userMacros) + "/MaxMCP_Launcher.ms"
    local barSys = (getDir #startupScripts) + "/MaxMCP_Toolbar.ms"
    local barUser = (getDir #userScripts) + "/Startup/MaxMCP_Toolbar.ms"
    local receipt = (getDir #userScripts) + "/MaxMCP/install_receipt.txt"
    print ("installed payload: " + ((doesFileExist payload) as string) + " " + payload)
    print ("installed macro (.mcr): " + ((doesFileExist macro) as string) + " " + macro)
    print ("stale macro (.ms):      " + ((doesFileExist macroStale) as string) + " (should be false)")
    print ("installed bar (system): " + ((doesFileExist barSys) as string) + " " + barSys)
    print ("installed bar (user):   " + ((doesFileExist barUser) as string) + " " + barUser)
    print ("install receipt:   " + ((doesFileExist receipt) as string) + " " + receipt)
    if (doesFileExist receipt) do
    (
        -- Handle closed on every path: a leaked handle plus a later
        -- collection is exactly the "MAXScript Garbage Collection Error".
        try
        (
            local fs = openFile receipt mode:"r"
            if (fs != undefined) do
            (
                try (print ("receipt says: " + (readLine fs))) catch ()
                try (close fs) catch ()
            )
        )
        catch ()
    )
    local dotnet = try ((dotNetClass "System.IO.File") != undefined) catch (false)
    print ("dotnet File API: " + (dotnet as string))
    local echoRC = try (hiddenDosCommand "cmd /c echo maxmcp-probe") catch ("THREW: " + (getCurrentException()))
    print ("shell echo exit: " + (echoRC as string))
    local probe = (getDir #userScripts) + "/MaxMCP_write_test.tmp"
    local wok = false
    local wdetail = ""
    try
    (
        (dotNetClass "System.IO.File").WriteAllText probe "probe"
        wok = doesFileExist probe
    )
    catch
    (
        wdetail = (getCurrentException())
    )
    try (deleteFile probe) catch ()
    print ("probe write userScripts: " + (wok as string) + " " + wdetail)
    local reg = try ((menuMan.createActionItem "MaxMCP_Launcher" "MCP Tools") != undefined) catch (false)
    print ("macro registered live: " + (reg as string))
    print "========== end diagnose: paste ALL lines above =========="
)
