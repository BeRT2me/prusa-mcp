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
    # Stats are written as comments in the gcode footer; read last 8KB to avoid
    # loading potentially large files into memory
    size = gcode_path.stat().st_size
    with gcode_path.open("rb") as f:
        f.seek(max(0, size - 8192))
        tail = f.read().decode(errors="replace")

    stats = SliceStats()
    for line in tail.splitlines():
        if not line.startswith(";"):
            continue
        if m := re.search(r"estimated printing time \(normal mode\) = (.+)", line):
            stats.print_time = m.group(1).strip()
        elif m := re.search(r"filament used \[g\] = ([\d.]+)", line):
            stats.filament_g = float(m.group(1))
        elif m := re.search(r"filament used \[cm3\] = ([\d.]+)", line):
            stats.filament_cm3 = float(m.group(1))
        elif m := re.search(r"total layers count = (\d+)", line):
            stats.layers = int(m.group(1))
    return stats
