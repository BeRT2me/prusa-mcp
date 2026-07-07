# PrusaSlicer MCP Server

An MCP server that lets Claude interact with PrusaSlicer — tweaking print settings, slicing models, and iterating — inspired by the Autodesk Fusion MCP integration.

## Status

**Working and committed to https://github.com/BeRT2me/prusa-mcp**

- ✅ FastMCP server with 10 tools (see below)
- ✅ 3MF read/write (`project.py`) — parses `; key = value` gcode-comment config format
- ✅ CLI subprocess wrapper + gcode stat parser (`slicer.py`)
- ✅ Config schema: 580 options, 97 help URLs extracted from PrusaSlicer source
- ✅ `get_config` annotates values with tooltips, units, valid values, help URLs
- ✅ `set_config` validates enum values and numeric ranges before writing
- ✅ `nil` values explained (e.g. `filament_retract_speed = nil` → inherits `retract_speed`)
- ✅ PrusaSlicer built from source at `~/claude-code/PrusaSlicer/build/src/prusa-slicer` (no GUI, static)
- ✅ `get_gui_state` reads running PrusaSlicer window title via PowerShell (Windows/WSL)
- ✅ `open_in_gui` guards against unsaved changes; uses `--single-instance` to reuse open window
- ✅ Native Windows support for `_cli_path`, `_datadir`, `open_in_gui`
- ✅ Slicer warnings surfaced in `SliceStats.warnings` (stdout blocks between `NN =>` progress lines)
- ✅ Stats include stealth-mode time, first-layer time, and filament cost (all from the gcode footer)
- ✅ `compare_stats` A/B-slices on temp copies — never mutates the project file
- ✅ `diff_config` vs load-time snapshot or vs a named preset
- ✅ Pytest suite: 87 tests, 100% coverage on `project.py` and `slicer.py`

## Next Steps

### 1. Wire into Claude Desktop
Add to `~/.config/claude-desktop/claude_desktop_config.json` (or platform equivalent):
```json
{
  "mcpServers": {
    "prusa-slicer": {
      "command": "uv",
      "args": ["run", "--directory", "/home/claude/claude-code/prusa-mcp", "prusa-mcp"]
    }
  }
}
```

### 2. GitHub Actions CI
Add `.github/workflows/test.yml` to run `uv run pytest --cov` on push. The test suite
is already in place — this is just the YAML wrapper.

### 3. README
The repo has no README yet. Should cover:
- What it is / demo workflow
- Prerequisites (PrusaSlicer built or installed, uv)
- Installation & Claude Desktop config
- Available tools
- How to refresh the config schema after PrusaSlicer updates

### 4. End-to-end test
- Load a real `.3mf` via `load_project`
- Have Claude read settings, propose changes, call `set_config`
- Call `slice_model`, verify stats come back
- Confirm `open_in_gui` round-trip

### 5. STL/OBJ import (planned)
New `import_model(path)` tool: run CLI `--export-3mf` to wrap the mesh in a project
`.3mf`, then load it. CLI-exported 3mfs have no `Slic3r_PE.config` — `write_config`
creates the member, and `load_preset` fills in real settings. The cube smoke test
already proved this pipeline end to end.

### 6. Model transforms (planned)
New `transform_model(scale=None, rotate_z=None, duplicate=None)` tool: CLI
`--scale/--rotate/--duplicate` + `--export-3mf` to a temp file, then atomic-replace
the project and reload config. **Verify first** whether CLI export preserves the
config member; if not, snapshot the config before and re-apply after. Guard on
GUI unsaved changes like `open_in_gui` does.

### 7. Per-object overrides (planned)
`Metadata/Slic3r_PE_model.config` is XML (per-object `<metadata>` elements), not the
`; key = value` format — needs ElementTree parsing in `project.py`. Tools:
`get_object_overrides()` / `set_object_override(object_id, settings)`, validation via
the existing `_validate`. Inspect a real GUI-saved multi-object 3mf before building.

### 8. Nice-to-haves (future)
- Post-slice thumbnail — PrusaSlicer can generate a preview of the sliced result; expose it
- Publish to PyPI so `uvx prusa-mcp` works in Claude Desktop configs (needs README + CI first)

---

## Workflow

1. User arranges models in PrusaSlicer GUI, saves as `.3mf`
2. Claude reads the `.3mf` (geometry + all settings + thumbnail)
3. Claude edits settings, rewrites the `.3mf`
4. `.3mf` auto-reopens in GUI so user can visually review
5. Either Claude (CLI) or the user (GUI) generates gcode

## Architecture

External Python MCP server — no C++ modifications to PrusaSlicer. The server shells out to the `prusa-slicer` CLI binary and manipulates `.3mf` files directly.

```
Claude
  ↕ MCP Protocol
MCP Server (Python)  [~/claude-code/prusa-mcp]
  - Maintains current project state (active .3mf path)
  - Reads/writes .3mf (ZIP containing XML + thumbnail)
  - Shells out to prusa-slicer CLI for slicing
  - Parses gcode output for print stats
  ↕ subprocess / filesystem
prusa-slicer CLI  +  PrusaSlicer GUI (shared ~/.config/PrusaSlicer)
```

## Key Technical Details

### 3MF is a ZIP
```
project.3mf
├── 3D/3dmodel.model              # geometry
├── Metadata/
│   ├── Slic3r_PE.config          # "; key = value" lines — what we read/write
│   ├── Slic3r_PE_model.config    # per-object setting overrides
│   └── thumbnail.png             # free visual preview for Claude
└── [Content_Types].xml
```

### CLI binary
Built at: `~/claude-code/PrusaSlicer/build/src/prusa-slicer`
Built without GUI (`-DSLIC3R_GUI=no`) — CLI only.
GUI version used for visualization (system install or separate build).

### Shared config
Both CLI and GUI share `~/.config/PrusaSlicer/` — printer/filament/print
presets set up in the GUI are available to the CLI automatically.

### Stats from gcode
After slicing, stats are in the gcode footer as comments. The parser reads the last
64 KB of the file (the config dump embedded in the footer can exceed 8 KB):
```
; estimated printing time (normal mode) = 2h 30m 45s
; filament used [g] = 23.45
; filament used [cm3] = 7.89
; total layers count = 142
```
PrusaSlicer 2.9+ (BGCode format) omits `total layers count` — the parser falls back
to counting `;LAYER_CHANGE` markers in the full file. `--no-binary-gcode` is passed
to the CLI to force text gcode output.

### Platform support
Windows, WSL, and Linux. macOS is deliberately unsupported (wrong datadir path,
no `xdg-open`) — decided in the 2026-07 review; revisit only if a mac user shows up.
`PRUSA_MCP_CLI`, `PRUSA_MCP_GUI`, and `PRUSA_MCP_DATADIR` env vars override path detection.

### GUI state detection
`get_gui_state` reads the PrusaSlicer window title via PowerShell on Windows/WSL.
Title format: `filename - PrusaSlicer-2.x.y based on Slic3r`
Unsaved changes: `*filename - PrusaSlicer-2.x.y based on Slic3r`
Not supported on plain Linux (no PowerShell).

### Config key names (verified against real .3mf)
Use `fill_density` and `fill_pattern` — NOT `infill_density`/`infill_pattern`.

### nil values
`filament_retract_*` settings use `nil` to mean "inherit from printer's `retract_*`".
`0` for extrusion widths and many speeds means "auto-calculate" — do NOT treat as zero.
These are intentional sentinel values, not missing data.

### Config schema
Extracted from PrusaSlicer source by `scripts/extract_config_schema.py`.
Re-run after PrusaSlicer updates:
```bash
uv run scripts/extract_config_schema.py ../PrusaSlicer/src/libslic3r/PrintConfig.cpp
```

## Test Suite

87 tests across three files, run with:
```bash
uv run pytest --cov --cov-report=term-missing
```

| File | Coverage | What it tests |
|---|---|---|
| `tests/test_project.py` | 100% | ZIP read/write, config parsing, round-trips |
| `tests/test_slicer.py` | 100% | Gcode stat parsing, LAYER_CHANGE fallback, CLI subprocess |
| `tests/test_server.py` | ~42% | Schema annotation, validation, INI parsing, `_read_gui_state` |

MCP tool handlers (`load_project`, `set_config`, etc.) are not unit tested — they involve
global `_project` state and real filesystem paths, making them integration-test territory.

Uses `pytest-mock` (`mocker` fixture) throughout — no `unittest.mock` imports in tests.

## MCP Tools

| Tool | Description |
|---|---|
| `load_project` | Load .3mf, return annotated key settings + thumbnail |
| `get_config` | Read settings with tooltips, units, valid values |
| `set_config` | Validate + update settings, write to .3mf |
| `slice_model` | Run CLI slicer, return times / filament / cost / layers / warnings |
| `compare_stats` | A/B-slice candidate settings on temp copies, return stats + delta |
| `diff_config` | Show settings changed this session, or vs a named preset |
| `get_gui_state` | Read running PrusaSlicer window title (Windows/WSL only) |
| `open_in_gui` | Reopen active .3mf in PrusaSlicer GUI (guards unsaved changes) |
| `list_presets` | List printer/filament/print presets from user config |
| `load_preset` | Apply a named preset to the active project |
