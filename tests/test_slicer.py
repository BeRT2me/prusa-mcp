"""Tests for slicer.py — gcode stat parsing and CLI subprocess wrapper."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from prusa_mcp import slicer

GCODE_TAIL = """\
; some unrelated comment
; estimated printing time (normal mode) = 2h 30m 45s
; filament used [g] = 23.45
; filament used [cm3] = 19.87
; total layers count = 142
"""


# --- _parse_stats ---


def test_parse_stats_all_fields(tmp_path: Path) -> None:
    gcode = tmp_path / "out.gcode"
    gcode.write_text(GCODE_TAIL)
    stats = slicer._parse_stats(gcode)
    assert stats.print_time == "2h 30m 45s"
    assert stats.filament_g == pytest.approx(23.45)
    assert stats.filament_cm3 == pytest.approx(19.87)
    assert stats.layers == 142


def test_parse_stats_partial(tmp_path: Path) -> None:
    # Stats are individually optional — partial output still parses without error
    gcode = tmp_path / "out.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m 0s\n")
    stats = slicer._parse_stats(gcode)
    assert stats.print_time == "1h 0m 0s"
    assert stats.filament_g is None
    assert stats.filament_cm3 is None
    assert stats.layers is None


def test_parse_stats_empty_file(tmp_path: Path) -> None:
    gcode = tmp_path / "out.gcode"
    gcode.write_text("")
    assert slicer._parse_stats(gcode) == slicer.SliceStats()


def test_parse_stats_ignores_non_comment_lines(tmp_path: Path) -> None:
    # Non-comment lines must not confuse the parser
    gcode = tmp_path / "out.gcode"
    gcode.write_text("G1 X10 Y10\ntotal layers count = 50\n; total layers count = 142\n")
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 142


def test_parse_stats_reads_tail_of_large_file(tmp_path: Path) -> None:
    # Stats live in the last ~8 KB; the function must seek rather than reading everything
    gcode = tmp_path / "out.gcode"
    padding = "G1 X0 Y0\n" * 2000  # ~20 KB of gcode body before stats
    gcode.write_text(padding + GCODE_TAIL)
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 142


# --- slice_project ---


async def test_slice_project_success(tmp_path: Path) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)  # pre-written so _parse_stats finds it

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        stats = await slicer.slice_project(
            project_path, output_path, cli_path=Path("prusa-slicer")
        )

    cmd = mock_exec.call_args[0]
    assert "--export-gcode" in cmd
    assert str(output_path) in cmd
    assert str(project_path) in cmd
    assert stats.layers == 142


async def test_slice_project_passes_datadir(tmp_path: Path) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)
    datadir = tmp_path / "config"

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await slicer.slice_project(
            project_path, output_path, cli_path=Path("prusa-slicer"), datadir=datadir
        )

    cmd = mock_exec.call_args[0]
    assert "--datadir" in cmd
    assert str(datadir) in cmd


async def test_slice_project_omits_datadir_when_none(tmp_path: Path) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        await slicer.slice_project(
            project_path, output_path, cli_path=Path("prusa-slicer"), datadir=None
        )

    cmd = mock_exec.call_args[0]
    assert "--datadir" not in cmd


async def test_slice_project_failure_raises(tmp_path: Path) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()

    mock_proc = AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = AsyncMock(return_value=(b"", b"slicing error"))

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        pytest.raises(RuntimeError, match="prusa-slicer failed"),
    ):
        await slicer.slice_project(
            project_path, tmp_path / "out.gcode", cli_path=Path("prusa-slicer")
        )
