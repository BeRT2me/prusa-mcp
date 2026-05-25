"""Tests for server.py — schema annotation, config validation, and INI parsing."""

from pathlib import Path

import pytest

from prusa_mcp import server

# --- _annotate ---


def test_annotate_no_schema() -> None:
    # Unknown keys pass through as bare value dicts — no KeyError
    assert server._annotate("unknown_key", "42") == {"value": "42"}


def test_annotate_includes_schema_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        server._SCHEMA,
        "_tkey",
        {"label": "Test Setting", "tooltip": "Does a thing", "sidetext": "mm"},
    )
    result = server._annotate("_tkey", "5.0")
    assert result["value"] == "5.0"
    assert result["label"] == "Test Setting"
    assert result["tooltip"] == "Does a thing"
    assert result["sidetext"] == "mm"


def test_annotate_nil_uses_nil_means(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_nilkey", {"nil_means": "Inherits retract_speed"})
    assert server._annotate("_nilkey", "nil")["note"] == "Inherits retract_speed"


def test_annotate_nil_default_note_when_nil_means_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_nilkey2", {})
    result = server._annotate("_nilkey2", "nil")
    assert "note" in result
    assert "Inherits" in result["note"]


def test_annotate_enum_builds_valid_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        server._SCHEMA,
        "_fill",
        {
            "enum_values": ["rectilinear", "gyroid"],
            "enum_labels": ["Rectilinear", "Gyroid"],
        },
    )
    result = server._annotate("_fill", "gyroid")
    assert "gyroid (Gyroid)" in result["valid_values"]
    assert "rectilinear (Rectilinear)" in result["valid_values"]


def test_annotate_enum_omits_parens_when_value_equals_label(monkeypatch: pytest.MonkeyPatch) -> None:
    # "auto (auto)" is redundant — the format should produce just "auto"
    monkeypatch.setitem(
        server._SCHEMA,
        "_same",
        {"enum_values": ["auto"], "enum_labels": ["auto"]},
    )
    assert server._annotate("_same", "auto")["valid_values"] == ["auto"]


def test_annotate_omits_unknown_schema_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the explicit allowlist of fields is forwarded — arbitrary keys must not leak through
    monkeypatch.setitem(server._SCHEMA, "_tkey2", {"label": "OK", "internal_only": "secret"})
    result = server._annotate("_tkey2", "1")
    assert "internal_only" not in result


# --- _validate ---


def test_validate_unknown_key_passes_through() -> None:
    # Unknown config keys are not rejected here — PrusaSlicer will catch them at slice time
    assert server._validate("totally_unknown_key", "anything") is None


def test_validate_nil_rejected_on_non_nullable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_nonil", {"nullable": False})
    assert server._validate("_nonil", "nil") is not None


def test_validate_nil_accepted_when_nullable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_yesnil", {"nullable": True})
    assert server._validate("_yesnil", "nil") is None


def test_validate_enum_rejects_bad_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_enum", {"enum_values": ["a", "b", "c"]})
    err = server._validate("_enum", "z")
    assert err is not None
    assert "a" in err  # error message lists valid options


def test_validate_enum_accepts_good_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_enum2", {"enum_values": ["gyroid", "rectilinear"]})
    assert server._validate("_enum2", "gyroid") is None


@pytest.mark.parametrize(
    ("value", "expected_fragment"),
    [
        ("0.05", "minimum"),   # below min
        ("1.5", "maximum"),    # above max
        ("0.3", None),         # in range
    ],
)
def test_validate_numeric_range(
    monkeypatch: pytest.MonkeyPatch, value: str, expected_fragment: str | None
) -> None:
    monkeypatch.setitem(server._SCHEMA, "_num", {"type": "float", "min": 0.1, "max": 1.0})
    result = server._validate("_num", value)
    if expected_fragment:
        assert expected_fragment in result
    else:
        assert result is None


def test_validate_percent_strips_symbol_before_range_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(server._SCHEMA, "_pct", {"type": "percent", "min": 0, "max": 100})
    assert server._validate("_pct", "15%") is None
    assert server._validate("_pct", "150%") is not None


def test_validate_nil_bypasses_numeric_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # nil is a valid sentinel for nullable numeric fields — must not raise a float() error
    monkeypatch.setitem(server._SCHEMA, "_nilnum", {"type": "float", "min": 0.0, "nullable": True})
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
