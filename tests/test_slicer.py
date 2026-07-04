"""Tests for slicer.py — gcode stat parsing and CLI subprocess wrapper."""

from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from prusa_mcp import slicer

GCODE_TAIL = """\
; some unrelated comment
; estimated printing time (normal mode) = 2h 30m 45s
; estimated printing time (stealth mode) = 3h 10m 0s
; estimated first layer printing time (normal mode) = 28s
; filament used [g] = 23.45
; filament used [cm3] = 19.87
; total filament cost = 0.85
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
    assert stats.print_time_stealth == "3h 10m 0s"
    assert stats.first_layer_time == "28s"
    assert stats.filament_g == pytest.approx(23.45)
    assert stats.filament_cm3 == pytest.approx(19.87)
    assert stats.cost == pytest.approx(0.85)
    assert stats.layers == 142


def test_parse_stats_multi_extruder_sums_values(tmp_path: Path) -> None:
    # Multi-tool prints report one comma-separated value per extruder — sum them.
    gcode = tmp_path / "out.gcode"
    gcode.write_text(
        "; filament used [g] = 10.5, 2.25\n"
        "; filament used [cm3] = 8.0, 1.5\n"
        "; total layers count = 10\n"
    )
    stats = slicer._parse_stats(gcode)
    assert stats.filament_g == pytest.approx(12.75)
    assert stats.filament_cm3 == pytest.approx(9.5)


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


# --- _parse_warnings ---


def test_parse_warnings_clean_run() -> None:
    stdout = "10 => Processing triangulated mesh\n90 => Exporting G-code\nSlicing result exported to /tmp/x\n"
    assert slicer._parse_warnings(stdout, "") == []


def test_parse_warnings_captures_warning_block() -> None:
    stdout = (
        "65 => Searching support spots\n"
        "print warning: Detected print stability issues:\n"
        "\n"
        "Low bed adhesion\n"
        "Consider enabling supports.\n"
        "89 => Calculating overhanging perimeters\n"
    )
    warnings = slicer._parse_warnings(stdout, "")
    assert len(warnings) == 1
    assert "Low bed adhesion" in warnings[0]
    assert "89 =>" not in warnings[0]


def test_parse_warnings_includes_stderr() -> None:
    warnings = slicer._parse_warnings("10 => Processing\n", "objects were loaded with custom supports\n")
    assert warnings == ["objects were loaded with custom supports"]


# --- stats_delta / _time_to_seconds ---


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("13m 40s", 820),
        ("2h 30m 45s", 9045),
        ("1d 1h 0m 1s", 90001),
        ("28s", 28),
        ("garbage", None),
        (None, None),
    ],
)
def test_time_to_seconds(text: str | None, seconds: int | None) -> None:
    assert slicer._time_to_seconds(text) == seconds


def test_stats_delta_all_fields() -> None:
    baseline = slicer.SliceStats(
        print_time="1h 0m 0s", filament_g=20.0, filament_cm3=8.0, cost=0.50, layers=100
    )
    candidate = slicer.SliceStats(
        print_time="45m 0s", filament_g=15.5, filament_cm3=6.0, cost=0.40, layers=100
    )
    delta = slicer.stats_delta(baseline, candidate)
    assert delta == {
        "print_time_seconds": -900,
        "filament_g": pytest.approx(-4.5),
        "filament_cm3": pytest.approx(-2.0),
        "cost": pytest.approx(-0.1),
        "layers": 0,
    }


def test_stats_delta_skips_missing_fields() -> None:
    # Fields absent on either side are omitted rather than reported as bogus zeros
    baseline = slicer.SliceStats(print_time="1h 0m 0s", filament_g=20.0, layers=100)
    candidate = slicer.SliceStats(filament_g=18.0)
    assert slicer.stats_delta(baseline, candidate) == {"filament_g": pytest.approx(-2.0)}


# --- slice_project ---


async def test_slice_project_success(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = mocker.AsyncMock(return_value=(b"", b""))
    mock_exec = mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    stats = await slicer.slice_project(project_path, output_path, cli_path=Path("prusa-slicer"))

    cmd = mock_exec.call_args[0]
    assert "--export-gcode" in cmd
    assert "--no-binary-gcode" in cmd  # required to force text gcode on PrusaSlicer 2.9+
    assert str(output_path) in cmd
    assert str(project_path) in cmd
    assert stats.layers == 142


async def test_slice_project_passes_datadir(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)
    datadir = tmp_path / "config"

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = mocker.AsyncMock(return_value=(b"", b""))
    mock_exec = mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    await slicer.slice_project(project_path, output_path, cli_path=Path("prusa-slicer"), datadir=datadir)

    cmd = mock_exec.call_args[0]
    assert "--datadir" in cmd
    assert str(datadir) in cmd


async def test_slice_project_omits_datadir_when_none(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = mocker.AsyncMock(return_value=(b"", b""))
    mock_exec = mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    await slicer.slice_project(project_path, output_path, cli_path=Path("prusa-slicer"), datadir=None)

    cmd = mock_exec.call_args[0]
    assert "--datadir" not in cmd


async def test_slice_project_returns_warnings(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()
    output_path = tmp_path / "out.gcode"
    output_path.write_text(GCODE_TAIL)

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate = mocker.AsyncMock(
        return_value=(b"10 => Processing\nprint warning: thin walls detected\n", b"")
    )
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    stats = await slicer.slice_project(project_path, output_path, cli_path=Path("prusa-slicer"))
    assert stats.warnings == ["print warning: thin walls detected"]


async def test_slice_project_failure_raises(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = mocker.AsyncMock(return_value=(b"", b"slicing error"))
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    with pytest.raises(RuntimeError, match="prusa-slicer failed"):
        await slicer.slice_project(
            project_path, tmp_path / "out.gcode", cli_path=Path("prusa-slicer")
        )


async def test_slice_project_failure_includes_stdout(tmp_path: Path, mocker: MockerFixture) -> None:
    # PrusaSlicer writes most diagnostics to stdout — the error must include both streams.
    project_path = tmp_path / "model.3mf"
    project_path.touch()

    mock_proc = mocker.AsyncMock()
    mock_proc.returncode = 1
    mock_proc.communicate = mocker.AsyncMock(return_value=(b"stdout detail", b"stderr detail"))
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)

    with pytest.raises(RuntimeError, match=r"(?s)stdout detail.*stderr detail"):
        await slicer.slice_project(
            project_path, tmp_path / "out.gcode", cli_path=Path("prusa-slicer")
        )


async def test_slice_project_timeout_kills_process(tmp_path: Path, mocker: MockerFixture) -> None:
    project_path = tmp_path / "model.3mf"
    project_path.touch()

    mock_proc = mocker.AsyncMock()
    mock_proc.kill = mocker.Mock()
    # Plain Mock — wait_for is patched to raise, so a real coroutine would never be
    # awaited and emit a RuntimeWarning.
    mock_proc.communicate = mocker.Mock()
    mocker.patch("asyncio.create_subprocess_exec", return_value=mock_proc)
    mocker.patch("prusa_mcp.slicer.asyncio.wait_for", side_effect=TimeoutError)

    with pytest.raises(RuntimeError, match="timed out"):
        await slicer.slice_project(
            project_path, tmp_path / "out.gcode", cli_path=Path("prusa-slicer"), timeout=1
        )
    mock_proc.kill.assert_called_once()
