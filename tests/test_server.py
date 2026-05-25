"""Tests for server.py — schema annotation, config validation, and INI parsing."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest_mock import MockerFixture

from prusa_mcp import server

# --- _annotate ---


def test_annotate_no_schema() -> None:
    # Unknown keys pass through as bare value dicts — no KeyError
    assert server._annotate("unknown_key", "42") == {"value": "42"}


def test_annotate_includes_schema_fields(mocker: MockerFixture) -> None:
    mocker.patch.dict(
        server._SCHEMA,
        {"_tkey": {"label": "Test Setting", "tooltip": "Does a thing", "sidetext": "mm"}},
    )
    result = server._annotate("_tkey", "5.0")
    assert result["value"] == "5.0"
    assert result["label"] == "Test Setting"
    assert result["tooltip"] == "Does a thing"
    assert result["sidetext"] == "mm"


def test_annotate_nil_uses_nil_means(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_nilkey": {"nil_means": "Inherits retract_speed"}})
    assert server._annotate("_nilkey", "nil")["note"] == "Inherits retract_speed"


def test_annotate_nil_default_note_when_nil_means_absent(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_nilkey2": {}})
    result = server._annotate("_nilkey2", "nil")
    assert "note" in result
    assert "Inherits" in result["note"]


def test_annotate_enum_builds_valid_values(mocker: MockerFixture) -> None:
    mocker.patch.dict(
        server._SCHEMA,
        {"_fill": {"enum_values": ["rectilinear", "gyroid"], "enum_labels": ["Rectilinear", "Gyroid"]}},
    )
    result = server._annotate("_fill", "gyroid")
    assert "gyroid (Gyroid)" in result["valid_values"]
    assert "rectilinear (Rectilinear)" in result["valid_values"]


def test_annotate_enum_omits_parens_when_value_equals_label(mocker: MockerFixture) -> None:
    # "auto (auto)" is redundant — the format should produce just "auto"
    mocker.patch.dict(server._SCHEMA, {"_same": {"enum_values": ["auto"], "enum_labels": ["auto"]}})
    assert server._annotate("_same", "auto")["valid_values"] == ["auto"]


def test_annotate_omits_unknown_schema_fields(mocker: MockerFixture) -> None:
    # Only the explicit allowlist of fields is forwarded — arbitrary keys must not leak through
    mocker.patch.dict(server._SCHEMA, {"_tkey2": {"label": "OK", "internal_only": "secret"}})
    result = server._annotate("_tkey2", "1")
    assert "internal_only" not in result


# --- _validate ---


def test_validate_unknown_key_passes_through() -> None:
    # Unknown config keys are not rejected here — PrusaSlicer will catch them at slice time
    assert server._validate("totally_unknown_key", "anything") is None


def test_validate_nil_rejected_on_non_nullable(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_nonil": {"nullable": False}})
    assert server._validate("_nonil", "nil") is not None


def test_validate_nil_accepted_when_nullable(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_yesnil": {"nullable": True}})
    assert server._validate("_yesnil", "nil") is None


def test_validate_enum_rejects_bad_value(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_enum": {"enum_values": ["a", "b", "c"]}})
    err = server._validate("_enum", "z")
    assert err is not None
    assert "a" in err  # error message lists valid options


def test_validate_enum_accepts_good_value(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_enum2": {"enum_values": ["gyroid", "rectilinear"]}})
    assert server._validate("_enum2", "gyroid") is None


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [
        ("0.05", "minimum"),   # below min
        ("1.5", "maximum"),    # above max
        ("0.3", None),         # in range
    ],
)
def test_validate_numeric_range(mocker: MockerFixture, value: str, expected_fragment: str | None) -> None:
    mocker.patch.dict(server._SCHEMA, {"_num": {"type": "float", "min": 0.1, "max": 1.0}})
    result = server._validate("_num", value)
    if expected_fragment:
        assert expected_fragment in result
    else:
        assert result is None


def test_validate_percent_strips_symbol_before_range_check(mocker: MockerFixture) -> None:
    mocker.patch.dict(server._SCHEMA, {"_pct": {"type": "percent", "min": 0, "max": 100}})
    assert server._validate("_pct", "15%") is None
    assert server._validate("_pct", "150%") is not None


def test_validate_nil_bypasses_numeric_check(mocker: MockerFixture) -> None:
    # nil is a valid sentinel for nullable numeric fields — must not raise a float() error
    mocker.patch.dict(server._SCHEMA, {"_nilnum": {"type": "float", "min": 0.0, "nullable": True}})
    assert server._validate("_nilnum", "nil") is None


# --- _parse_ini ---


def test_parse_ini_basic(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("[print]\nlayer_height = 0.2\nfill_density = 15%\n")
    assert server._parse_ini(ini) == {"layer_height": "0.2", "fill_density": "15%"}


def test_parse_ini_skips_section_headers(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("[section]\nkey = val\n")
    assert server._parse_ini(ini) == {"key": "val"}


def test_parse_ini_skips_hash_comments(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("# comment\nkey = val\n")
    assert server._parse_ini(ini) == {"key": "val"}


def test_parse_ini_skips_semicolon_comments(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("; comment\nkey = val\n")
    assert server._parse_ini(ini) == {"key": "val"}


def test_parse_ini_empty(tmp_path: Path) -> None:
    ini = tmp_path / "empty.ini"
    ini.write_text("")
    assert server._parse_ini(ini) == {}


def test_parse_ini_skips_lines_without_equals(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("no_equals_here\nkey = val\n")
    assert server._parse_ini(ini) == {"key": "val"}


def test_parse_ini_strips_whitespace(tmp_path: Path) -> None:
    ini = tmp_path / "preset.ini"
    ini.write_text("  key  =  value  \n")
    assert server._parse_ini(ini) == {"key": "value"}


# --- _read_gui_state ---


def _ps_result(stdout: str) -> SimpleNamespace:
    """Minimal stand-in for subprocess.CompletedProcess — only .stdout is accessed."""
    return SimpleNamespace(stdout=stdout)


def test_read_gui_state_returns_none_on_plain_linux(mocker: MockerFixture) -> None:
    # Linux without WSL has no PowerShell — function returns None immediately.
    mocker.patch("prusa_mcp.server.platform.system", return_value="Linux")
    mocker.patch.object(server, "_is_wsl", return_value=False)
    assert server._read_gui_state() is None


def test_read_gui_state_parses_open_file(mocker: MockerFixture) -> None:
    mocker.patch("prusa_mcp.server.platform.system", return_value="Windows")
    mocker.patch("prusa_mcp.server.subprocess.run", return_value=_ps_result(
        "mymodel - PrusaSlicer-2.8.0 based on Slic3r\n"
    ))
    assert server._read_gui_state() == {"open_file": "mymodel", "unsaved_changes": False}


def test_read_gui_state_detects_unsaved_changes(mocker: MockerFixture) -> None:
    # PrusaSlicer prefixes the title with "*" when there are unsaved changes.
    mocker.patch("prusa_mcp.server.platform.system", return_value="Windows")
    mocker.patch("prusa_mcp.server.subprocess.run", return_value=_ps_result(
        "*mymodel - PrusaSlicer-2.8.0 based on Slic3r\n"
    ))
    assert server._read_gui_state() == {"open_file": "mymodel", "unsaved_changes": True}


def test_read_gui_state_no_process_returns_none(mocker: MockerFixture) -> None:
    # Empty stdout means PrusaSlicer is not running.
    mocker.patch("prusa_mcp.server.platform.system", return_value="Windows")
    mocker.patch("prusa_mcp.server.subprocess.run", return_value=_ps_result(""))
    assert server._read_gui_state() is None


def test_read_gui_state_unrecognised_title_returns_none(mocker: MockerFixture) -> None:
    # A title that doesn't match the "... - PrusaSlicer" pattern must not crash.
    mocker.patch("prusa_mcp.server.platform.system", return_value="Windows")
    mocker.patch("prusa_mcp.server.subprocess.run", return_value=_ps_result(
        "Some Other Application\n"
    ))
    assert server._read_gui_state() is None


def test_read_gui_state_subprocess_error_returns_none(mocker: MockerFixture) -> None:
    mocker.patch("prusa_mcp.server.platform.system", return_value="Windows")
    mocker.patch("prusa_mcp.server.subprocess.run", side_effect=OSError("no powershell"))
    assert server._read_gui_state() is None
