# prusa-mcp

An MCP server that lets Claude interact with PrusaSlicer — reading and tweaking print settings, slicing models, and iterating on results.

Inspired by the [Autodesk Fusion MCP](https://github.com/autodesk-platform-services/aps-mcp-server) integration.

## Workflow

1. Arrange models in PrusaSlicer GUI, save as `.3mf`
2. Ask Claude to load the file and review settings
3. Claude proposes changes, calls `set_config` to apply them
4. File auto-reopens in PrusaSlicer GUI for visual review
5. Slice via CLI (Claude) or GUI (you), compare stats

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- PrusaSlicer — either:
  - System install (CLI available as `prusa-slicer` on PATH), or
  - Built from source (see [PrusaSlicer build docs](https://github.com/prusa3d/PrusaSlicer/blob/master/doc/How%20to%20build%20-%20Linux%20et%20al.md))

## Installation

```bash
git clone https://github.com/BeRT2me/prusa-mcp
cd prusa-mcp
uv sync
```

## Claude Desktop Setup

Add to your `claude_desktop_config.json`:

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`  
**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`  
**Linux:** `~/.config/claude-desktop/claude_desktop_config.json`

```json
{
  "mcpServers": {
    "prusa-slicer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/prusa-mcp", "prusa-mcp"]
    }
  }
}
```

Replace `/path/to/prusa-mcp` with the actual clone path. Restart Claude Desktop after saving.

## Tools

| Tool | Description |
|---|---|
| `load_project` | Load a `.3mf` file and return annotated key settings + thumbnail |
| `get_config` | Read settings with tooltips, units, and valid values |
| `set_config` | Validate and update settings, write to `.3mf` |
| `slice_model` | Run the CLI slicer, return print time / filament / layer count |
| `open_in_gui` | Reopen the active `.3mf` in PrusaSlicer GUI |
| `list_presets` | List available printer / filament / print presets |
| `load_preset` | Apply a named preset to the active project |

## Config Schema

The bundled `config_schema.json` covers 580 PrusaSlicer options with tooltips, units, valid values, and help URLs — extracted directly from the PrusaSlicer source. Claude uses this to understand and validate every setting it touches.

To refresh the schema after a PrusaSlicer update:

```bash
uv run scripts/extract_config_schema.py           # fetches latest release from GitHub
uv run scripts/extract_config_schema.py --ref v2.9.0  # specific version
uv run scripts/extract_config_schema.py path/to/PrintConfig.cpp  # local source tree
```
