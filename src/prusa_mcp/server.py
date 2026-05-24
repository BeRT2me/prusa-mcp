"""PrusaSlicer MCP server."""

import base64
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import project, slicer

mcp = FastMCP("prusa-slicer")


@dataclass
class _ProjectState:
    # Single active project — this server is inherently single-user/single-session
    path: Path | None = None
    config: dict[str, str] = field(default_factory=dict)

    def require(self) -> None:
        if self.path is None:
            msg = "No project loaded. Call load_project first."
            raise ValueError(msg)


_project = _ProjectState()

# Config schema extracted from PrintConfig.cpp + Tab.cpp — may be absent on first run
_SCHEMA_PATH = Path(__file__).parent / "config_schema.json"
_SCHEMA: dict[str, dict] = (
    json.loads(_SCHEMA_PATH.read_text(encoding="utf-8")) if _SCHEMA_PATH.exists() else {}
)


# --- Tools ---


@mcp.tool()
def load_project(path: str) -> str:
    """Load a .3mf project file and read its print settings.

    Returns a summary of the loaded project with annotated key settings.
    Also provides the embedded thumbnail as a base64 PNG for visual context.
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        msg = f"File not found: {path}"
        raise ValueError(msg)
    if p.suffix.lower() != ".3mf":
        msg = f"Expected a .3mf file, got: {p.suffix}"
        raise ValueError(msg)

    _project.path = p
    _project.config = project.read_config(p)

    thumbnail = project.read_thumbnail(p)
    thumb_info = ""
    if thumbnail:
        b64 = base64.b64encode(thumbnail).decode()
        thumb_info = f"\nthumbnail_png_base64: {b64}"

    summary = {k: _annotate(k, _project.config[k]) for k in _SUMMARY_KEYS if k in _project.config}
    return f"Loaded: {p.name}\n{json.dumps(summary, indent=2)}{thumb_info}"


@mcp.tool()
def get_config(keys: list[str] | None = None) -> str:
    """Get print settings from the active project with descriptions and valid values.

    Pass a list of keys to retrieve specific settings, or omit to get all settings.
    Each value is annotated with its label, units, valid range, and tooltip.
    """
    _project.require()
    items = (
        _project.config
        if keys is None
        else {k: _project.config[k] for k in keys if k in _project.config}
    )
    return json.dumps({k: _annotate(k, v) for k, v in items.items()}, indent=2)


@mcp.tool()
def set_config(settings: dict[str, str]) -> str:
    """Update one or more print settings in the active project.

    Changes are validated against the config schema and written immediately to the .3mf file.
    Example: {"layer_height": "0.3", "fill_density": "20%", "fill_pattern": "gyroid"}
    """
    _project.require()

    errors = {k: msg for k in settings if (msg := _validate(k, settings[k]))}
    if errors:
        msg = "Validation errors:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items())
        raise ValueError(msg)

    _project.config.update(settings)
    project.write_config(_project.path, _project.config)  # type: ignore[arg-type]
    return f"Updated: {', '.join(f'{k}={v}' for k, v in settings.items())}"


@mcp.tool()
async def slice_model(output_path: str | None = None) -> str:
    """Slice the active project and return print statistics.

    Writes gcode to output_path, or next to the .3mf if not specified.
    Returns estimated print time, filament usage, and layer count.
    """
    _project.require()
    out = Path(output_path) if output_path else _project.path.with_suffix(".gcode")  # type: ignore[union-attr]
    stats = await slicer.slice_project(
        _project.path,  # type: ignore[arg-type]
        out,
        cli_path=_cli_path(),
        datadir=_datadir(),
    )
    return json.dumps(stats.model_dump(), indent=2)


@mcp.tool()
def open_in_gui() -> str:
    """Reopen the active project in PrusaSlicer GUI for visual review."""
    _project.require()
    subprocess.Popen(["xdg-open", str(_project.path)])  # noqa: S603, S607
    return f"Opened {_project.path.name} in GUI"  # type: ignore[union-attr]


@mcp.tool()
def list_presets(preset_type: str) -> str:
    """List available presets by type: 'printer', 'filament', or 'print'."""
    if preset_type not in {"printer", "filament", "print"}:
        msg = "preset_type must be 'printer', 'filament', or 'print'"
        raise ValueError(msg)
    preset_dir = _datadir() / preset_type
    if not preset_dir.exists():
        return json.dumps([])
    names = [p.stem for p in sorted(preset_dir.glob("*.ini"))]
    return json.dumps(names, indent=2)


@mcp.tool()
def load_preset(preset_type: str, name: str) -> str:
    """Apply a saved preset to the active project.

    Merges the preset's settings into the current config and writes to disk.
    preset_type: 'printer', 'filament', or 'print'
    """
    _project.require()
    if preset_type not in {"printer", "filament", "print"}:
        msg = "preset_type must be 'printer', 'filament', or 'print'"
        raise ValueError(msg)

    preset_path = _datadir() / preset_type / f"{name}.ini"
    if not preset_path.exists():
        msg = f"Preset not found: {preset_type}/{name}"
        raise ValueError(msg)

    preset_config = _parse_ini(preset_path)
    _project.config.update(preset_config)
    project.write_config(_project.path, _project.config)  # type: ignore[arg-type]
    return f"Applied {preset_type} preset '{name}' ({len(preset_config)} settings)"


# --- Schema helpers ---


def _annotate(key: str, value: str) -> dict:
    """Combine a raw config value with its schema metadata for Claude's context."""
    entry: dict = {"value": value}
    info = _SCHEMA.get(key, {})

    for f in ("label", "sidetext", "tooltip", "min", "max", "help_url"):
        if f in info:
            entry[f] = info[f]

    if value == "nil":
        entry["note"] = info.get("nil_means", "Inherits from a parent setting at slice time")

    if "enum_values" in info:
        labels = info.get("enum_labels", info["enum_values"])
        entry["valid_values"] = [
            f"{v} ({lbl})" if v != lbl else v
            for v, lbl in zip(info["enum_values"], labels, strict=True)
        ]

    return entry


def _validate(key: str, value: str) -> str | None:
    """Return an error message if value is invalid for key, else None."""
    info = _SCHEMA.get(key)
    if not info:
        return None  # Unknown key — pass through, PrusaSlicer will catch it

    if value == "nil" and not info.get("nullable"):
        return "not nullable — use a concrete value or omit"

    if "enum_values" in info and value not in info["enum_values"]:
        return f"must be one of: {', '.join(info['enum_values'])}"

    if info.get("type") in {"float", "int", "percent"} and value != "nil":
        try:
            num = float(value.rstrip("%"))
            if "min" in info and num < info["min"]:
                return f"minimum is {info['min']}"
            if "max" in info and num > info["max"]:
                return f"maximum is {info['max']}"
        except ValueError:
            pass

    return None


# --- Other helpers ---


def _cli_path() -> Path:
    # Prefer the locally built binary; fall back to system PATH
    built = Path.home() / "claude-code/PrusaSlicer/build/src/prusa-slicer"
    if built.exists():
        return built
    return Path("prusa-slicer")


def _datadir() -> Path:
    return Path.home() / ".config/PrusaSlicer"


def _parse_ini(path: Path) -> dict[str, str]:
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[")):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip()
    return result


# Settings shown in the load_project summary
_SUMMARY_KEYS = [
    "layer_height",
    "fill_density",
    "fill_pattern",
    "perimeters",
    "support_material",
    "print_speed",
    "temperature",
    "bed_temperature",
    "printer_model",
    "filament_type",
    "print_settings_id",
    "filament_settings_id",
]


def main() -> None:
    mcp.run()
