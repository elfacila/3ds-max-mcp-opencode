# Skills Registry (`skills_registry.py`, v1.1.0)

Transport-free skill store. Skills know nothing about sockets or Qt; the SAME function runs from the UI and from MCP dispatch — see [[System-Map]].

## Core API

- `@register_skill(name, category, description, params, title)` — validates schema (`str|int|float|bool|choice|node`), rejects duplicates. Source auto-attributed: `__module__ == skills_registry` → `"builtin"`, else the custom module name.
- `execute_skill(name, params)` — coerces params, calls body, always returns JSON-safe `{"ok", "skill", ...}`. Never raises.
- `validate_params()` / `_coerce_value()` — defaults merge + type coercion (`choice` has case-insensitive fallback; `node` passes name strings through).
- `build_skill_code(name, params)` — remote snippet (`import skills_registry; __mcp_result__ = execute_skill(...)`) for the `{"code"}` envelope in [[Protocol]].
- Built-ins: `scene_cleaner` (Scene Management), `batch_modifier_applier` (Geometry & Modifiers), `camera_light_setup` (Materials & Cameras). Dry-run outside Max.

## Custom skills subsystem

- Folder: `custom_skills_dir()` → `<module>/custom_skills/` (`MAXMCP_CUSTOM_SKILLS` overrides for tests); auto-created on reload.
- `reload_skills()` — `ast.parse` gate per file (3.7 grammar enforced via `feature_version` on newer hosts), `spec_from_file_location` import, vanished files drop their skills, built-ins untouchable, collisions reported as errors. Report shape in [[Data-Structures]].
- `skill_template()` — 3.7-clean starter (guarded `rt` import, honest `NotImplementedError` stub, dry-run echo).
- `extract_skill_metadata()` / `validate_skill_source()` / `skill_source_path()` — feed the [[GUI-Plugin]] editor and Edit button.
- Autoload: `reload_skills()` runs once at import (`AUTOLOAD_REPORT`); UI hot-reload calls the same function, so UI and MCP dispatch always agree.

## Tests

25-assert headless suite: template round-trip, reload/update/delete, syntax + import-error collection, builtin-collision rejection, helper ignoring, node coercion. Built-ins keep the 5-test `--test` suite green.

Related: [[System-Map]], [[Data-Structures]], [[GUI-Plugin]], [[Install-Guide]], [[Dev-Log]].
