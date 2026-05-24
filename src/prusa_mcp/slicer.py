"""PrusaSlicer CLI wrapper."""

import asyncio
import re
from pathlib import Path

from pydantic import BaseModel


class SliceStats(BaseModel):
    print_time: str | None = None
    filament_g: float | None = None
    filament_cm3: float | None = None
    layers: int | None = None
    max_layer_z: float | None = None


async def slice_project(
    project_path: Path,
    output_path: Path,
    *,
    cli_path: Path,
    datadir: Path | None = None,
) -> SliceStats:
    cmd = [str(cli_path), "--export-gcode", "--output", str(output_path)]
    if datadir:
        cmd += ["--datadir", str(datadir)]
    cmd.append(str(project_path))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        msg = f"prusa-slicer failed (exit {proc.returncode}):\n{stderr.decode()}"
        raise RuntimeError(msg)

    return _parse_stats(output_path)


def _parse_stats(gcode_path: Path) -> SliceStats:
    with gcode_path.open("rb") as f:
        raw = f.read()

    # Binary BGCode (Core One / MK4) stores stats as null-delimited key=value pairs.
    # Text gcode stores them as "; key = value" comment lines.
    # Normalise to a single flat string by replacing non-printable bytes with spaces.
    printable = frozenset(range(32, 127)) | {9, 10, 13}
    text = "".join(chr(b) if b in printable else " " for b in raw)

    stats = SliceStats()
    # Text gcode: "; estimated printing time (normal mode) = 9m 0s"
    # Binary gcode: " time (normal mode)=9m 0s"
    if m := re.search(r"time \(normal mode\)[= ]+([0-9dhms ]+)", text):
        stats.print_time = m.group(1).strip()
    if m := re.search(r"filament used \[g\][= ]+([\d.]+)", text):
        stats.filament_g = float(m.group(1))
    if m := re.search(r"filament used \[cm3\][= ]+([\d.]+)", text):
        stats.filament_cm3 = float(m.group(1))
    if m := re.search(r"total layers count[= ]+(\d+)", text):
        stats.layers = int(m.group(1))
    # Binary gcode encodes layer count as max_layer_z; fall back if layers missing
    if stats.layers is None and (m := re.search(r"max_layer_z[= ]+([\d.]+)", text)):
        stats.max_layer_z = float(m.group(1))
    return stats
