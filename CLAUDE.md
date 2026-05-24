# PrusaSlicer MCP Server

An MCP server that lets Claude interact with PrusaSlicer — tweaking print settings, slicing models, and iterating — inspired by the Autodesk Fusion MCP integration.

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
MCP Server (Python)
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
│   ├── Slic3r_PE.config          # print/filament/printer settings
│   ├── Slic3r_PE_model.config    # per-object setting overrides
│   └── thumbnail.png             # free visual preview for Claude
└── [Content_Types].xml
```

### CLI binary
Built at: `~/claude-code/PrusaSlicer/build/src/prusa-slicer`
Built without GUI (`-DSLIC3R_GUI=no`) — CLI only.
GUI version (system install or separate build) used for visualization.

### Shared config
Both CLI and GUI share `~/.config/PrusaSlicer/` — printer/filament/print
presets set up in the GUI are available to the CLI automatically.

### Stats from gcode
After slicing, stats are written as comments at the top of the gcode file:
```
; estimated printing time (normal mode) = 2h 30m 45s
; filament used [g] = 23.45
; filament used [cm3] = 7.89
; total layers count = 142
```

### Reopening in GUI
```bash
prusa-slicer --single-instance project.3mf  # reuse existing window if open
# fallback:
xdg-open project.3mf
```

## Planned MCP Tools

| Tool | Description |
|---|---|
| `load_project` | Set active .3mf, extract and return settings + thumbnail |
| `get_config` | Read current print/filament/printer settings |
| `set_config` | Modify one or more settings in the active .3mf |
| `slice` | Run CLI slicer on active .3mf, return print stats |
| `save_gcode` | Write gcode to a specified path |
| `save_project` | Write modified .3mf to disk |
| `open_in_gui` | Reopen active .3mf in PrusaSlicer GUI |
| `list_presets` | List available printer/filament/print presets |
| `load_preset` | Apply a named preset to the active project |

## Key Settings Claude Will Tweak

```ini
layer_height          # 0.1–0.4mm — quality vs speed
fill_density          # 0–100% (note: not infill_density)
fill_pattern          # gyroid, grid, honeycomb, etc. (note: not infill_pattern)
perimeters            # wall count — affects strength
support_material      # on/off
print_speed           # mm/s
temperature           # nozzle
first_layer_temperature
bed_temperature
cooling               # fan settings
```

## Coding Notes

- Python, async where applicable (subprocess, file I/O)
- Pydantic models at MCP boundaries (tool inputs/outputs)
- No GUI interaction — all state lives in the .3mf and config files
- Use `--datadir ~/.config/PrusaSlicer` so CLI uses real user presets
