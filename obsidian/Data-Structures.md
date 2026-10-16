# Data Structures

Canonical shapes flowing between the nodes in [[System-Map]]. Field names are exact.

## SkillSpec (`skills_registry.py`)

```python
SkillSpec(
    name="extrude_walls",      # id: [A-Za-z_][A-Za-z0-9_]*
    title="Extrude Walls",     # display label in tree
    category="Geometry",       # tree group
    description="...",
    params=[                   # param dicts (types: str|int|float|bool|choice|node)
        {"name": "height", "type": "float", "default": 3000.0,
         "label": "Height", "min": ..., "max": ..., "help": ...},
    ],
    func=<callable>,           # the skill body
    source="builtin"           # or "maxmcp_custom_<stem>" for file skills
)
```

- Store: `SKILL_REGISTRY: {name -> SkillSpec}`. Remote dispatch reads it **live**, so reloads need no restart — see [[Skills-Registry]].
- `node` params travel as **name strings**, resolved inside the skill body.

## McpBridgeServer (`max_mcp_ui.py`)

- State: `running`, `active_clients` (lock-guarded), `total_requests`, `queue_depth`.
- Queue items: `(request_dict, holder)` where `holder = {"event": threading.Event, "response": dict}`.
- Threads: daemon accept thread = network only; `QTimer` (300 ms) `pump_once()` on the main thread = all `pymxs`/`exec`. See [[GUI-Plugin]].

## Protocol envelopes (see [[Protocol]])

- Requests: `{"id", "code"}` (legacy) or `{"id", "skill"|"tool", "params"|"arguments"|"args"}`.
- Responses: `{"id", "status": "ok"|"error", "output", "result", "error", "version"}`.

## reload_skills() report

`{"ok", "dir", "loaded": [...], "removed": [...], "errors": {file: traceback}, "total": N}` — rendered into the console by the UI. See [[Skills-Registry]].

## File layout (project root → scene)

```
3dsmax-mcp/
├── max_mcp_ui.py / skills_registry.py   # shipped sources (see [[GUI-Plugin]], [[Skills-Registry]])
├── custom_skills/                       # dev-time skill drafts (env override MAXMCP_CUSTOM_SKILLS)
├── installer/MaxMCP/                    # MZP payload staging (see [[Installer]])
├── installer/MaxMCP_Plugin.mzp          # 9-file install archive
├── max_listener.py / mcp_server_3dsmax.py / test_mcp.py   # see [[Bridge-Legacy]]
└── obsidian/                            # this vault
        ↓ install ↓
#userScripts/MaxMCP/
├── max_mcp_ui.py / skills_registry.py
└── custom_skills/*.py                   # user-managed, hot-reloadable
#userMacros/MaxMCP_Launcher.ms  # #userIcons/MaxMCP_{16,24}i.bmp  #startupScripts/MaxMCP_Toolbar.ms
```

Related: [[System-Map]], [[Install-Guide]].
