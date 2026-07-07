"""PrusaSlicer CLI wrapper."""

import asyncio
import re
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel


class SliceStats(BaseModel):
    print_time: str | None = None
    print_time_stealth: str | None = None
    first_layer_time: str | None = None
    filament_g: float | None = None
    filament_cm3: float | None = None
    cost: float | None = None
    layers: int | None = None
    warnings: list[str] = []


async def slice_project(
    project_path: Path,
    output_path: Path,
    *,
    cli_path: Path,
    datadir: Path | None = None,
    timeout: float = 600,  # noqa: ASYNC109 — deliberate: callers are MCP tools, not asyncio code
) -> SliceStats:
    cmd = [str(cli_path), "--export-gcode", "--no-binary-gcode", "--output", str(output_path)]
    if datadir:
        cmd += ["--datadir", str(datadir)]
    cmd.append(str(project_path))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        msg = f"prusa-slicer timed out after {timeout:.0f}s slicing {project_path.name}"
        raise RuntimeError(msg) from None

    if proc.returncode != 0:
        # PrusaSlicer writes most diagnostics to stdout, not stderr
        msg = (
            f"prusa-slicer failed (exit {proc.returncode}):\n"
            f"{stdout.decode(errors='replace')}\n{stderr.decode(errors='replace')}"
        )
        raise RuntimeError(msg)

    stats = _parse_stats(output_path)
    stats.warnings = _parse_warnings(
        stdout.decode(errors="replace"), stderr.decode(errors="replace")
    )
    return stats


def stats_delta(baseline: SliceStats, candidate: SliceStats) -> dict[str, float | int]:
    """Numeric differences (candidate - baseline) for fields present in both."""
    delta: dict[str, float | int] = {}
    b, c = _time_to_seconds(baseline.print_time), _time_to_seconds(candidate.print_time)
    if b is not None and c is not None:
        delta["print_time_seconds"] = c - b
    for f in ("filament_g", "filament_cm3", "cost"):
        bv, cv = getattr(baseline, f), getattr(candidate, f)
        if bv is not None and cv is not None:
            delta[f] = round(cv - bv, 3)
    if baseline.layers is not None and candidate.layers is not None:
        delta["layers"] = candidate.layers - baseline.layers
    return delta


# Progress lines ("45 => Making infill") and the final export line; anything else
# on stdout of a successful run is a warning ("print warning: ..." blocks etc.)
_PROGRESS = re.compile(r"^\d+ => |^Slicing result exported")


def _parse_warnings(stdout: str, stderr: str) -> list[str]:
    lines = [ln for ln in stdout.splitlines() if ln.strip() and not _PROGRESS.match(ln)]
    blocks = []
    if lines:
        blocks.append("\n".join(lines))
    if stderr.strip():
        blocks.append(stderr.strip())
    return blocks


def _time_to_seconds(t: str | None) -> int | None:
    """Parse PrusaSlicer time strings like "2h 30m 45s" or "1d 2h 3m"."""
    if not t:
        return None
    parts = re.findall(r"(\d+)\s*([dhms])", t)
    if not parts:
        return None
    mult = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(int(n) * mult[u] for n, u in parts)


def _sum_values(raw: str) -> float | None:
    values = re.findall(r"\d+(?:\.\d+)?", raw)
    return sum(float(v) for v in values) if values else None


# Footer comment patterns → (SliceStats field, converter). _sum_values handles the
# comma-separated per-extruder form of multi-tool prints.
_STAT_PATTERNS: list[tuple[re.Pattern[str], str, Callable[[str], object]]] = [
    (re.compile(r"estimated printing time \(normal mode\) = (.+)"), "print_time", str.strip),
    (re.compile(r"estimated printing time \(stealth mode\) = (.+)"), "print_time_stealth", str.strip),
    (
        re.compile(r"estimated first layer printing time \(normal mode\) = (.+)"),
        "first_layer_time",
        str.strip,
    ),
    (re.compile(r"filament used \[g\] = (.+)"), "filament_g", _sum_values),
    (re.compile(r"filament used \[cm3\] = (.+)"), "filament_cm3", _sum_values),
    (re.compile(r"total filament cost = ([\d.]+)"), "cost", float),
    (re.compile(r"total layers count = (\d+)"), "layers", int),
]


def _parse_stats(gcode_path: Path) -> SliceStats:
    # Stats are written before the config dump in the gcode footer.
    # The config dump can be >8KB, so read the last 64KB to be safe.
    size = gcode_path.stat().st_size
    with gcode_path.open("rb") as f:
        f.seek(max(0, size - 65536))
        tail = f.read().decode(errors="replace")

    stats = SliceStats()
    for line in tail.splitlines():
        if not line.startswith(";"):
            continue
        for pattern, field, convert in _STAT_PATTERNS:
            if m := pattern.search(line):
                setattr(stats, field, convert(m.group(1)))
                break

    # PrusaSlicer 2.9+ omits "total layers count"; count ;LAYER_CHANGE markers instead.
    if stats.layers is None:
        full = gcode_path.read_bytes()
        stats.layers = full.count(b";LAYER_CHANGE")

    return stats
