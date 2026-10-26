# System Map

Hub node of the vault. Every component below links back here, so Graph view renders the full dependency web. Arrows (`→`) mean "calls / feeds into".

## Runtime graph

```
OpenCode agent
    │  MCP/stdio (3 tools: get_scene_info, execute_python, create_primitive)
    ▼
[[Bridge-Legacy|mcp_server_3dsmax.py]] ──TCP pinned instance──▶ [[Bridge-Legacy|max_listener.py]] (3ds Max, pump=ctypes)
                                                              │  (endpoint file + instance_id token;
                                                              │   last Start wins, others ignored)
                                                              │
OpenCode ──MCP skill dispatch──▶ [[GUI-Plugin|McpBridgeServer]] (daemon thread + QTimer pump) ──exec──▶ [[Skills-Registry|execute_skill()]]
                                                                                                        ▲
[[GUI-Plugin|MaxMcpWindow]] ──Add/Import/Edit/Reload──▶ custom_skills/*.py ──reload_skills()──▶ [[Skills-Registry|SKILL_REGISTRY]]
        │                                                                              │
        └─ Execute Locally ──▶ execute_skill() ◀── remote dispatch ─────────────────────┘
        │
        ├─ checkboxes/profile bar ──▶ enablement + profiles_config.json ──▶ [[Skills-Registry|tools_version]]
        │                                                                     │
MCP host ──tools/list + await_change──▶ enabled-only catalog ◀────────────────┘
```

## Node catalogue

| Node | File | Role | Links |
|---|---|---|---|
| Agent entry | OpenCode | sends MCP tool calls | [[Protocol]], [[Bridge-Legacy]] |
| Bridge server | `mcp_server_3dsmax.py` | MCP tools → TCP | [[Bridge-Legacy]], [[Protocol]] |
| Legacy listener | `max_listener.py` | in-Max exec, ctypes pump | [[Bridge-Legacy]], [[Protocol]] |
| GUI panel | [[GUI-Plugin]] | listener + browser + console | [[Skills-Registry]], [[Protocol]], [[Installer]] |
| Skill store | [[Skills-Registry]] | registry + autoload + reload | [[GUI-Plugin]], [[Data-Structures]] |
| Custom skills | `custom_skills/*.py` | user skills, hot-reloadable | [[Skills-Registry]], [[GUI-Plugin]] |
| Installer | [[Installer]] | MZP, macro, toolbar, icons | [[GUI-Plugin]], [[Install-Guide]] |
| Wire format | [[Protocol]] | envelopes for code/skill/catalog | [[Data-Structures]], [[Bridge-Legacy]] |
| Profiles | `profiles_config.json` | enabled set + named presets | [[Skills-Registry]], [[GUI-Plugin]], [[Protocol]] |

## Data-flow edges (for graph reading)

- [[GUI-Plugin]] → [[Skills-Registry]] (imports, calls `execute_skill`, `reload_skills`, profile API)
- [[Skills-Registry]] → custom skills (imports `custom_skills/*.py` at load + reload)
- [[Installer]] → [[GUI-Plugin]] (deploys `max_mcp_ui.py`, launches via `toggle_mcp_ui`)
- [[Protocol]] → [[GUI-Plugin]] + [[Bridge-Legacy]] (both speak the same envelopes)
- Profiles → [[Protocol]] (enablement filters `tools/list`; version bumps wake `await_change`)
- Instance pinning → [[Protocol]] (`hello` identity; endpoint file elects the linked Max; token verified pre-flight + per response)
- [[Data-Structures]] describes the payloads on every edge above
- [[Dev-Log]] records when each edge was built; [[Install-Guide]] is the deploy path
