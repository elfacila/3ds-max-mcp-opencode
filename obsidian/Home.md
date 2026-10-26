# MaxMCP Knowledge Vault

Obsidian vault for the **MaxMCP** project — MCP bridge + GUI plugin + skills system for **3ds Max 2022**. Open this `obsidian/` folder as a vault in Obsidian and use **Graph view** to see how everything connects.

## Start here

- [[System-Map]] — the hub node: every component and how they talk to each other (best viewed in Graph view)
- [[Data-Structures]] — registry, server, protocol envelopes, file layout
- [[Install-Guide]] — one-click install, toolbar, skills folder
- [[Dev-Log]] — session-by-session build history

## Components

- [[GUI-Plugin]] — `max_mcp_ui.py`: panel, listener, skill manager
- [[Skills-Registry]] — `skills_registry.py`: registry, custom skills, hot-reload
- [[Installer]] — `.mzp` package, MacroScript, toolbar, icons
- [[Protocol]] — TCP wire format and dispatch paths
- [[Bridge-Legacy]] — the original `max_listener.py` / `mcp_server_3dsmax.py` pair

## Quick status

Listener `1.4.3` (legacy) live on `127.0.0.1:8080` · UI `1.4.3` (dockable, reload-proof toggle) · Skills `1.4.3` (empty by design) · Installer `1.4.3` (10-file MZP, toolbar opt-in) · repo: `elfacila/3ds-max-mcp-opencode` (`master`).
