macroScript MaxMCP_Launcher
    category:"MCP Tools"
    toolTip:"MCP Bridge: Server + Skills"
    buttonText:"Max MCP"
    Icon:#("MaxMCP", 1)
(
    on execute do
    (
        local rawDir = (getDir #userScripts) + "\\MaxMCP"
        local pluginDir = substituteString rawDir "\\" "/"
        local mainPy = pluginDir + "/max_mcp_ui.py"

        if not (doesFileExist mainPy) then
        (
            messageBox ("MaxMCP plugin files not found in:\n" + pluginDir + \
                "\n\nRe-run install.ms (or drop the .mzp again).") title:"Max MCP" beep:true
            return undefined
        )

        local pyCmd = "import sys\n" + \
                      "_p = '" + pluginDir + "'\n" + \
                      "if _p not in sys.path:\n" + \
                      "    sys.path.insert(0, _p)\n" + \
                      "try:\n" + \
                      "    import importlib\n" + \
                      "    if 'skills_registry' in sys.modules:\n" + \
                      "        import skills_registry\n" + \
                      "        importlib.reload(skills_registry)\n" + \
                      "    if 'max_mcp_ui' in sys.modules:\n" + \
                      "        import max_mcp_ui\n" + \
                      "        importlib.reload(max_mcp_ui)\n" + \
                      "    else:\n" + \
                      "        import max_mcp_ui\n" + \
                      "    max_mcp_ui.toggle_mcp_ui()\n" + \
                      "except Exception as _e:\n" + \
                      "    import traceback\n" + \
                      "    traceback.print_exc()\n" + \
                      "    print('[MaxMCP] launch failed: ' + str(_e))\n"

        python.Execute pyCmd
    )
)
