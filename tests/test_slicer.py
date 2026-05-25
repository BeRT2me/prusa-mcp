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

# Tail without an explicit layer count — triggers the ;LAYER_CHANGE fallback
GCODE_TAIL_NO_LAYERS = """\
; estimated printing time (normal mode) = 2h 30m 45s
; filament used [g] = 23.45
; filament used [cm3] = 19.87
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
    # Non-layer stats are individually optional; layers falls back to 0 via LAYER_CHANGE count.
    gcode = tmp_path / "out.gcode"
    gcode.write_text("; estimated printing time (normal mode) = 1h 0m 0s\n")
    stats = slicer._parse_stats(gcode)
    assert stats.print_time == "1h 0m 0s"
    assert stats.filament_g is None
    assert stats.filament_cm3 is None
    assert stats.layers == 0  # no ;LAYER_CHANGE markers → fallback returns 0


def test_parse_stats_empty_file(tmp_path: Path) -> None:
    gcode = tmp_path / "out.gcode"
    gcode.write_text("")
    assert slicer._parse_stats(gcode) == slicer.SliceStats(layers=0)


def test_parse_stats_ignores_non_comment_lines(tmp_path: Path) -> None:
    gcode = tmp_path / "out.gcode"
    gcode.write_text("G1 X10 Y10\ntotal layers count = 50\n; total layers count = 142\n")
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 142


def test_parse_stats_reads_tail_of_large_file(tmp_path: Path) -> None:
    # Stats live in the last 64 KB; verify they're found even with >64 KB of gcode before them.
    gcode = tmp_path / "out.gcode"
    padding = "G1 X0 Y0\n" * 8000  # ~80 KB — exceeds the 64 KB seek window
    gcode.write_text(padding + GCODE_TAIL)
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 142


# --- _parse_stats: PrusaSlicer 2.9+ LAYER_CHANGE fallback ---


def test_parse_stats_layer_change_fallback(tmp_path: Path) -> None:
    # PrusaSlicer 2.9+ omits "total layers count"; count ;LAYER_CHANGE markers instead.
    gcode = tmp_path / "out.gcode"
    gcode.write_bytes(b";LAYER_CHANGE\n" * 100 + GCODE_TAIL_NO_LAYERS.encode())
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 100


def test_parse_stats_explicit_count_beats_layer_change(tmp_path: Path) -> None:
    # When "total layers count" is present, it wins over ;LAYER_CHANGE counting.
    gcode = tmp_path / "out.gcode"
    # 50 markers but explicit count says 142
    gcode.write_bytes(b";LAYER_CHANGE\n" * 50 + GCODE_TAIL.encode())
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 142


def test_parse_stats_layer_change_counts_full_file(tmp_path: Path) -> None:
    # LAYER_CHANGE markers are at the top of the gcode, well outside the 64 KB tail window.
    # The fallback reads the full file, so they must still be counted correctly.
    gcode = tmp_path / "out.gcode"
    header = b";LAYER_CHANGE\n" * 200          # 200 markers at the top
    padding = b"G1 X0 Y0\n" * 7000            # ~70 KB filler
    gcode.write_bytes(header + padding + GCODE_TAIL_NO_LAYERS.encode())
    stats = slicer._parse_stats(gcode)
    assert stats.layers == 200


# --- slice_project ---


async def test_slice_project_success(tmp_path: Path) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = AsyncMock(return_value=(b"", b""))

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec:
        stats = await slicer.slice_project(
            project_path, output_path, cli_path=Path("prusa-slicer")
        )

    cmd = mock_exec.call_args[0]
    assert "--export-gcode" in cmd
    assert "--no-binary-gcode" in cmd  # required to force text gcode on PrusaSlicer 2.9+
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
