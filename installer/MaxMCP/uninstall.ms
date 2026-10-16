/* ==========================================================================
   uninstall.ms — drag into a viewport to remove the MaxMCP plugin.
   Suppresses install.ms's auto-install, loads its definitions from the
   installed copy, then runs the uninstaller (button bar, menu entry and
   installed files are removed; the macro fully unloads on next restart).
   ========================================================================== */
global MaxMCP_autoInstall = false

-- NOTE: `local` is illegal at file top level in MAXScript, so the body
-- lives inside this parenthesized block.
(
    local instPath = (getDir #userScripts) + "/MaxMCP/install.ms"
    if (doesFileExist instPath) then
    (
        try (fileIn instPath) catch ()
        try (MaxMCP_uninstall()) catch
        (
            messageBox "MaxMCP uninstall failed (see Listener). Remove files manually." title:"MaxMCP"
        )
    )
    else
    (
        messageBox ("MaxMCP installer copy not found:\n" + instPath + \
            "\nNothing to uninstall.") title:"MaxMCP"
    )
)
