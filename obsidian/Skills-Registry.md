# Skills Registry (`skills_registry.py`, v1.4.1)

Transport-free skill store. Skills know nothing about sockets or Qt; the SAME function runs from the UI and from MCP dispatch — see [[System-Map]].

## Core API

- `@register_skill(name, category, description, params, title)` — validates schema (`str|int|float|bool|choice|node`), rejects duplicates. Source auto-attributed: `__module__ == skills_registry` → `"builtin"`, else the custom module name.
- `execute_skill(name, params, enforce_enabled=True)` — coerces params, calls body, always returns JSON-safe `{"ok", "skill", ...}`. Never raises. Remote paths enforce the profile gate (disabled → clear error); the UI passes `False` for explicit local clicks.
- `validate_params()` / `_coerce_value()` — defaults merge + type coercion (`choice` has case-insensitive fallback; `node` passes name strings through).
- `build_skill_code(name, params)` — remote snippet (`import skills_registry; __mcp_result__ = execute_skill(...)`) for the `{"code"}` envelope in [[Protocol]].
- Built-ins: none — the registry starts empty by design (v1.4.0+). `KNOWN_CATEGORIES` (Scene Management, Geometry & Modifiers, Materials & Cameras) keeps the UI folders stable for manual sorting. Test skills in `custom_skills/`: `extrude_splines`, `floor_slab`, `drop_to_ground`, `selection_report`, `tint_selection`. Outside Max every skill runs its dry-run echo.

## Profiles & enablement

- File: `profiles_config.json` (`MAXMCP_PROFILES` override); atomic writes; corrupt file → backed up + defaults restored, never raises.
- API: `save_profile` (strict, refuses All Skills), `load_profile`, `delete_profile` (All Skills protected; deleting the active one falls back to All), `get_all_profiles` (All first), `apply_profile` (drops vanished names with report).
- Defaults: All Skills (dynamic), Floor Plan Shell (resolves against registered skills in the modeling + cleanup categories — empty with no custom skills), Minimal / None (empty). `profiles_config.example.json` is the tracked template; the live file is git-ignored.
- Live: `is_skill_enabled` / `set_skill_enabled` / `set_all_enabled`; `tools_version()` + `on_tools_changed()` drive [[Protocol]] catalog sync. Schema in [[Data-Structures]].

## Custom skills subsystem

- Folder: `custom_skills_dir()` → `<module>/custom_skills/` (`MAXMCP_CUSTOM_SKILLS` overrides for tests); auto-created on reload.
- `reload_skills()` — `ast.parse` gate per file (3.7 grammar enforced via `feature_version` on newer hosts), `spec_from_file_location` import, vanished files drop their skills, built-ins untouchable, collisions reported as errors. Report shape in [[Data-Structures]].
- `skill_template()` — 3.7-clean starter (guarded `rt` import, honest `NotImplementedError` stub, dry-run echo).
- `extract_skill_metadata()` / `validate_skill_source()` / `skill_source_path()` — feed the [[GUI-Plugin]] editor and Edit button.
- Autoload: `reload_skills()` runs once at import (`AUTOLOAD_REPORT`); UI hot-reload calls the same function, so UI and MCP dispatch always agree.

## Tests

15-PASS headless suite: temp-skill dispatch, tool/arguments aliases, legacy code path, unknown/malformed errors, profile apply + `tools/list` + disabled-refusal + local override, `await_change` stale + parked-wake, CRUD guards. Customs in `custom_skills/` stay loaded (All-count is dynamic).

Related: [[System-Map]], [[Data-Structures]], [[GUI-Plugin]], [[Install-Guide]], [[Dev-Log]].
