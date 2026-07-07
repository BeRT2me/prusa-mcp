"""Tests for project.py — .3mf ZIP manipulation and config parsing."""

import io
import zipfile
from pathlib import Path

import pytest

from prusa_mcp import project


def _3mf_without_config(tmp_path: Path) -> Path:
    """Build a .3mf (e.g. from a non-PrusaSlicer exporter) that has no Slic3r_PE.config."""
    p = tmp_path / "foreign.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("3D/3dmodel.model", "<model/>")
    p.write_bytes(buf.getvalue())
    return p

# --- _parse_config ---


def test_parse_config_basic() -> None:
    raw = "; layer_height = 0.2\n; fill_density = 15%\n"
    assert project._parse_config(raw) == {"layer_height": "0.2", "fill_density": "15%"}


def test_parse_config_ignores_non_comment_lines() -> None:
    # Lines not starting with ";" are not settings — only the "; key = val" form counts
    raw = "layer_height = 0.2\n; real = value\n"
    assert project._parse_config(raw) == {"real": "value"}


def test_parse_config_strips_whitespace() -> None:
    raw = ";  key  =  value  \n"
    assert project._parse_config(raw) == {"key": "value"}


def test_parse_config_empty() -> None:
    assert project._parse_config("") == {}


def test_parse_config_skips_lines_without_equals() -> None:
    raw = "; just a note\n; key = val\n"
    assert project._parse_config(raw) == {"key": "val"}


def test_parse_config_value_may_contain_equals() -> None:
    # partition("=") splits on the first "=" only — values like "a=b" must survive
    raw = "; wipe_tower_x = 170.0\n"
    assert project._parse_config(raw) == {"wipe_tower_x": "170.0"}


# --- _serialize_config ---


def test_serialize_config_sorted() -> None:
    result = project._serialize_config({"b": "2", "a": "1"})
    value_lines = [ln for ln in result.splitlines() if "=" in ln]
    assert value_lines[0] == "; a = 1"
    assert value_lines[1] == "; b = 2"


def test_serialize_config_round_trips() -> None:
    config = {"layer_height": "0.2", "fill_pattern": "gyroid", "perimeters": "3"}
    assert project._parse_config(project._serialize_config(config)) == config


# --- read_config ---


def test_read_config(minimal_3mf: Path) -> None:
    config = project.read_config(minimal_3mf)
    assert config["fill_density"] == "15%"
    assert config["fill_pattern"] == "gyroid"
    assert config["layer_height"] == "0.2"
    assert config["perimeters"] == "3"


# --- read_thumbnail ---


def test_read_thumbnail_absent(minimal_3mf: Path) -> None:
    assert project.read_thumbnail(minimal_3mf) is None


def test_read_thumbnail_present(minimal_3mf_with_thumbnail: Path) -> None:
    data = project.read_thumbnail(minimal_3mf_with_thumbnail)
    assert data is not None
    assert data.startswith(b"\x89PNG")


# --- write_config ---


def test_write_config_round_trip(minimal_3mf: Path) -> None:
    config = project.read_config(minimal_3mf)
    config["layer_height"] = "0.3"
    config["injected_key"] = "injected_value"
    project.write_config(minimal_3mf, config)

    reread = project.read_config(minimal_3mf)
    assert reread["layer_height"] == "0.3"
    assert reread["injected_key"] == "injected_value"


def test_write_config_preserves_other_zip_members(minimal_3mf: Path) -> None:
    # The ZIP rebuild must not drop non-config entries like the geometry file
    project.write_config(minimal_3mf, {"key": "val"})
    with zipfile.ZipFile(minimal_3mf) as z:
        assert "3D/3dmodel.model" in z.namelist()


def test_write_config_overwrites_existing_value(minimal_3mf: Path) -> None:
    project.write_config(minimal_3mf, {"fill_pattern": "rectilinear"})
    assert project.read_config(minimal_3mf)["fill_pattern"] == "rectilinear"


def test_write_config_creates_member_when_absent(tmp_path: Path) -> None:
    # A .3mf without Slic3r_PE.config must gain one — not silently drop the write
    p = _3mf_without_config(tmp_path)
    project.write_config(p, {"layer_height": "0.2"})
    assert project.read_config(p) == {"layer_height": "0.2"}
    with zipfile.ZipFile(p) as z:
        assert "3D/3dmodel.model" in z.namelist()


def test_read_config_missing_member_raises_value_error(tmp_path: Path) -> None:
    p = _3mf_without_config(tmp_path)
    with pytest.raises(ValueError, match=r"Slic3r_PE\.config"):
        project.read_config(p)
