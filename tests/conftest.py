"""Shared fixtures for the prusa-mcp test suite."""

import io
import zipfile
from pathlib import Path

import pytest

SAMPLE_CONFIG = """\
; PrusaSlicer generated file

; fill_density = 15%
; fill_pattern = gyroid
; layer_height = 0.2
; perimeters = 3
; support_material = 0
; temperature = 215
"""


@pytest.fixture()
def minimal_3mf(tmp_path: Path) -> Path:
    """Minimal valid .3mf with a config member and a geometry stub."""
    p = tmp_path / "test.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/Slic3r_PE.config", SAMPLE_CONFIG)
        z.writestr("3D/3dmodel.model", "<model/>")
    p.write_bytes(buf.getvalue())
    return p


@pytest.fixture()
def minimal_3mf_with_thumbnail(tmp_path: Path) -> Path:
    """Minimal valid .3mf that includes an embedded thumbnail."""
    p = tmp_path / "with_thumb.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Metadata/Slic3r_PE.config", SAMPLE_CONFIG)
        z.writestr("Metadata/thumbnail.png", b"\x89PNG\r\n\x1a\n")
    p.write_bytes(buf.getvalue())
    return p
