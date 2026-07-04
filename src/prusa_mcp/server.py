"""PrusaSlicer MCP server."""

import functools
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from . import project, slicer

mcp = FastMCP("prusa-slicer")


@dataclass
class _ProjectState:
    # Single active project — this server is inherently single-user/single-session
    path: Path | None = None
    config: dict[str, str] = field(default_factory=dict)
    original_config: dict[str, str] = field(default_factory=dict)

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


@mcp.tool(structured_output=False)
def load_project(path: str) -> list[str | Image]:
    """Load a .3mf project file and read its print settings.

    Returns a summary of the loaded project with annotated key settings,
    plus the embedded thumbnail image for visual context.
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
    _project.original_config = dict(_project.config)

    summary = {k: _annotate(k, _project.config[k]) for k in _SUMMARY_KEYS if k in _project.config}
    result: list[str | Image] = [f"Loaded: {p.name}\n{json.dumps(summary, indent=2)}"]
    if thumbnail := project.read_thumbnail(p):
        result.append(Image(data=thumbnail, format="png"))
    return result


@mcp.tool()
def get_config(keys: list[str] | None = None) -> str:
    """Get print settings from the active project.

    Pass a list of keys to retrieve specific settings, annotated with label, units,
    valid range, and tooltip. Omit keys to get all settings as bare key/value pairs
    (unannotated — the full annotated dump would be enormous).
    """
    _project.require()
    if keys is None:
        return json.dumps(_project.config, indent=2)

    result: dict = {k: _annotate(k, _project.config[k]) for k in keys if k in _project.config}
    if missing := [k for k in keys if k not in _project.config]:
        if not result:
            msg = f"No such settings in project: {', '.join(missing)}"
            raise ValueError(msg)
        result["_missing"] = missing
    return json.dumps(result, indent=2)


@mcp.tool()
def set_config(settings: dict[str, str]) -> str:
    """Update one or more print settings in the active project.

    Changes are validated against the config schema and written immediately to the .3mf file.
    Example: {"layer_height": "0.3", "fill_density": "20%", "fill_pattern": "gyroid"}
    """
    _project.require()
    _validate_settings(settings)

    # Re-read from disk so external edits (e.g. GUI saves) aren't clobbered,
    # and only update in-memory state after the write succeeds.
    config = project.read_config(_project.path)  # type: ignore[arg-type]
    config.update(settings)
    project.write_config(_project.path, config)  # type: ignore[arg-type]
    _project.config = config
    return f"Updated: {', '.join(f'{k}={v}' for k, v in settings.items())}"


@mcp.tool()
async def slice_model(output_path: str | None = None) -> str:
    """Slice the active project and return print statistics.

    Writes gcode to output_path, or next to the .3mf if not specified.
    Returns estimated print time, filament usage, and layer count.
    """
    _project.require()
    out = (
        Path(output_path).expanduser().resolve()  # noqa: ASYNC240 — trivial next to the slice itself
        if output_path
        else _project.path.with_suffix(".gcode")  # type: ignore[union-attr]
    )
    stats = await slicer.slice_project(
        _project.path,  # type: ignore[arg-type]
        out,
        cli_path=_cli_path(),
        datadir=_datadir(),
    )
    return json.dumps(stats.model_dump(), indent=2)


@mcp.tool()
async def compare_stats(settings: dict[str, str]) -> str:
    """Slice the project with and without candidate settings and compare the results.

    Both slices run against temporary copies — the project file and its config are
    never modified. Returns baseline stats, candidate stats, and their delta.
    Example: {"fill_pattern": "grid", "fill_density": "10%"}
    """
    _project.require()
    _validate_settings(settings)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        candidate_3mf = tmp / _project.path.name  # type: ignore[union-attr]
        shutil.copy2(_project.path, candidate_3mf)  # type: ignore[arg-type]
        config = project.read_config(candidate_3mf)
        config.update(settings)
        project.write_config(candidate_3mf, config)

        baseline = await slicer.slice_project(
            _project.path,  # type: ignore[arg-type]
            tmp / "baseline.gcode",
            cli_path=_cli_path(),
            datadir=_datadir(),
        )
        candidate = await slicer.slice_project(
            candidate_3mf,
            tmp / "candidate.gcode",
            cli_path=_cli_path(),
            datadir=_datadir(),
        )

    result = {
        "changed_settings": settings,
        "baseline": baseline.model_dump(),
        "candidate": candidate.model_dump(),
        "delta": slicer.stats_delta(baseline, candidate),
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def diff_config(preset_type: str | None = None, name: str | None = None) -> str:
    """Show settings that differ from a reference config.

    With no arguments, compares against the settings as they were when load_project
    was called — i.e. what has changed this session. Pass preset_type ('printer',
    'filament', or 'print') and name to compare against a saved preset instead;
    only the keys present in the preset are compared.
    """
    _project.require()
    if (preset_type is None) != (name is None):
        msg = "Pass both preset_type and name, or neither"
        raise ValueError(msg)

    if preset_type is None:
        keys = _project.original_config.keys() | _project.config.keys()
        diff = {
            k: {"was": _project.original_config.get(k), "now": _project.config.get(k)}
            for k in sorted(keys)
            if _project.original_config.get(k) != _project.config.get(k)
        }
    else:
        preset_path = _preset_dir(preset_type) / f"{name}.ini"
        if not preset_path.exists():
            msg = f"Preset not found: {preset_type}/{name}"
            raise ValueError(msg)
        preset = _parse_ini(preset_path)
        preset.pop("inherits", None)
        diff = {
            k: {"preset": v, "project": _project.config.get(k)}
            for k, v in sorted(preset.items())
            if _project.config.get(k) != v
        }
    return json.dumps(diff, indent=2)


@mcp.tool()
def get_gui_state() -> str:
    """Return the current state of the running PrusaSlicer GUI instance.

    Reports whether PrusaSlicer is open, which file is shown in the title bar,
    whether it has unsaved changes, and whether that file matches the active
    project loaded in this MCP server.

    Call this before open_in_gui to confirm the right file is already open,
    or to warn the user if they have unsaved changes they should save first.
    """
    state = _read_gui_state()
    if state is None:
        return json.dumps({"running": False})

    result: dict = {"running": True, **state}
    if _project.path is not None:
        result["matches_project"] = state["open_file"] == _project.path.stem
    return json.dumps(result, indent=2)


@mcp.tool()
def open_in_gui() -> str:
    """Reopen the active project in PrusaSlicer GUI for visual review.

    Expects PrusaSlicer to already be running with the model open.
    When the dialog appears, the user should choose "Import config only"
    to apply Claude's setting changes without disturbing the current model view.
    """
    _project.require()

    # Refuse to overwrite unsaved GUI edits — the user must save in PrusaSlicer first.
    state = _read_gui_state()
    if state and state.get("unsaved_changes"):
        msg = (
            "PrusaSlicer has unsaved changes. "
            "Please save the project in PrusaSlicer first, then call open_in_gui again."
        )
        raise ValueError(msg)

    gui = _gui_path()
    if gui is not None:
        # --single-instance forwards the file to an already-open PrusaSlicer window
        # instead of spawning a new process each time.
        subprocess.Popen([str(gui), "--single-instance", str(_project.path)])  # noqa: S603
    else:
        match platform.system():
            case "Windows":
                os.startfile(str(_project.path))  # noqa: S606
            case _:  # Linux / macOS / WSL
                if _is_wsl():
                    win_path = subprocess.check_output(  # noqa: S603
                        ["wslpath", "-w", str(_project.path)],  # noqa: S607
                        text=True,
                    ).strip()
                    subprocess.Popen(["cmd.exe", "/c", "start", "", win_path])  # noqa: S603, S607
                else:
                    subprocess.Popen(["xdg-open", str(_project.path)])  # noqa: S603, S607
    return f"Opened {_project.path.name} in GUI"  # type: ignore[union-attr]


@mcp.tool()
def list_presets(preset_type: str) -> str:
    """List available presets by type: 'printer', 'filament', or 'print'."""
    preset_dir = _preset_dir(preset_type)
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
    preset_path = _preset_dir(preset_type) / f"{name}.ini"
    if not preset_path.exists():
        msg = f"Preset not found: {preset_type}/{name}"
        raise ValueError(msg)

    preset_config = _parse_ini(preset_path)
    # User presets are often deltas over a system profile — the parent's settings
    # are not in the .ini, so the merge below may be incomplete.
    inherits = preset_config.pop("inherits", "")

    config = project.read_config(_project.path)  # type: ignore[arg-type]
    config.update(preset_config)
    project.write_config(_project.path, config)  # type: ignore[arg-type]
    _project.config = config

    result = f"Applied {preset_type} preset '{name}' ({len(preset_config)} settings)"
    if inherits:
        result = (
            f"Warning: this preset inherits from '{inherits}' — only its overrides were "
            f"applied; inherited settings are unchanged.\n{result}"
        )
    return result


# --- Schema helpers ---


def _validate_settings(settings: dict[str, str]) -> None:
    errors = {k: msg for k in settings if (msg := _validate(k, settings[k]))}
    if errors:
        msg = "Validation errors:\n" + "\n".join(f"  {k}: {v}" for k, v in errors.items())
        raise ValueError(msg)


def _preset_dir(preset_type: str) -> Path:
    if preset_type not in {"printer", "filament", "print"}:
        msg = "preset_type must be 'printer', 'filament', or 'print'"
        raise ValueError(msg)
    return _datadir() / preset_type


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
            f"{v} ({lbl})" if v != lbl else v for v, lbl in zip(info["enum_values"], labels, strict=True)
        ]

    return entry


def _validate(key: str, value: str) -> str | None:
    """Return an error message if value is invalid for key, else None."""
    info = _SCHEMA.get(key)
    if not info:
        return None  # Unknown key — pass through, PrusaSlicer will catch it

    if value == "nil":
        return None if info.get("nullable") else "not nullable — use a concrete value or omit"

    if "enum_values" in info and value not in info["enum_values"]:
        return f"must be one of: {', '.join(info['enum_values'])}"

    match info.get("type"):
        case "float" | "int" | "percent" | "float_or_percent":
            return _validate_number(info, value)
        case "bool":
            return None if value in {"0", "1"} else "must be 0 or 1"
    return None


def _validate_number(info: dict, value: str) -> str | None:
    try:
        num = float(value.rstrip("%"))
    except ValueError:
        return f"not a valid number: {value!r}"
    if "min" in info and num < info["min"]:
        return f"minimum is {info['min']}"
    if "max" in info and num > info["max"]:
        return f"maximum is {info['max']}"
    return None


# --- Other helpers ---


@functools.cache
def _is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        return False


@functools.cache
def _win_appdata() -> Path | None:
    # Resolve %APPDATA% via cmd.exe and convert to a WSL-accessible path.
    # Cached — only runs once per server process.
    try:
        raw = subprocess.check_output(
            ["cmd.exe", "/c", "echo %APPDATA%"],  # noqa: S607
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        wsl = subprocess.check_output(["wslpath", raw], text=True).strip()  # noqa: S603, S607
        return Path(wsl)
    except (subprocess.SubprocessError, FileNotFoundError):
        return None


# Typical Windows PrusaSlicer install locations, as WSL paths
_WIN_PRUSA_PATHS = [
    Path("/mnt/c/Program Files/Prusa3D/PrusaSlicer/prusa-slicer.exe"),
    Path("/mnt/c/Program Files (x86)/Prusa3D/PrusaSlicer/prusa-slicer.exe"),
]

# Typical Windows PrusaSlicer install locations, as native Windows paths
_WIN_PRUSA_PATHS_NATIVE = [
    Path("C:/Program Files/Prusa3D/PrusaSlicer/prusa-slicer.exe"),
    Path("C:/Program Files (x86)/Prusa3D/PrusaSlicer/prusa-slicer.exe"),
]


def _read_gui_state() -> dict | None:
    """Return {open_file, unsaved_changes} from the running PrusaSlicer window title, or None."""
    if platform.system() == "Windows":
        ps_cmd = "powershell"
    elif _is_wsl():
        ps_cmd = "powershell.exe"
    else:
        return None  # Linux without WSL — not supported yet

    try:
        ps_query = (
            "Get-Process prusa-slicer -ErrorAction SilentlyContinue"
            " | Select-Object -ExpandProperty MainWindowTitle"
        )
        result = subprocess.run(  # noqa: S603
            [ps_cmd, "-Command", ps_query],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None

    title = result.stdout.strip().strip("'")
    if not title:
        return None

    # Title format: "filename - PrusaSlicer-2.x.y based on Slic3r"
    #           or: "*filename - PrusaSlicer-2.x.y based on Slic3r" (unsaved changes)
    m = re.match(r"^(\*)?(.+?)\s+-\s+PrusaSlicer", title)
    if not m:
        return None
    return {"open_file": m.group(2).strip(), "unsaved_changes": m.group(1) == "*"}


def _gui_path() -> Path | None:
    """Return the PrusaSlicer GUI binary path, or None if not found."""
    if env := os.environ.get("PRUSA_MCP_GUI"):
        return Path(env)
    if platform.system() == "Windows":
        for p in _WIN_PRUSA_PATHS_NATIVE:
            if p.exists():
                return p
    elif _is_wsl():
        for p in _WIN_PRUSA_PATHS:
            if p.exists():
                return p
    return None


def _cli_path() -> Path:
    if env := os.environ.get("PRUSA_MCP_CLI"):
        return Path(env)
    if platform.system() == "Windows":
        for p in _WIN_PRUSA_PATHS_NATIVE:
            if p.exists():
                return p
        return Path("prusa-slicer")  # fall back to PATH
    # Prefer a locally built binary, then Windows install (WSL), then system PATH
    built = Path.home() / "claude-code/PrusaSlicer/build/src/prusa-slicer"
    if built.exists():
        return built
    if _is_wsl():
        for p in _WIN_PRUSA_PATHS:
            if p.exists():
                return p
    return Path("prusa-slicer")


def _datadir() -> Path:
    if env := os.environ.get("PRUSA_MCP_DATADIR"):
        return Path(env)
    if platform.system() == "Windows":
        return Path(os.environ["APPDATA"]) / "PrusaSlicer"
    if _is_wsl():
        appdata = _win_appdata()
        if appdata:
            return appdata / "PrusaSlicer"
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
