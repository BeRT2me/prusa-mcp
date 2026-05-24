"""Read and write PrusaSlicer .3mf project files."""

import io
import zipfile
from pathlib import Path

_PRINT_CONFIG = "Metadata/Slic3r_PE.config"
_THUMBNAIL = "Metadata/thumbnail.png"


def read_config(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        content = z.read(_PRINT_CONFIG).decode()
    return _parse_config(content)


def read_thumbnail(path: Path) -> bytes | None:
    with zipfile.ZipFile(path) as z:
        if _THUMBNAIL in z.namelist():
            return z.read(_THUMBNAIL)
    return None


def write_config(path: Path, config: dict[str, str]) -> None:
    _replace_zip_member(path, _PRINT_CONFIG, _serialize_config(config).encode())


def _parse_config(content: str) -> dict[str, str]:
    result = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith(";"):
            continue
        line = line[1:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def _serialize_config(config: dict[str, str]) -> str:
    lines = ["; PrusaSlicer generated file", ""]
    for key, value in sorted(config.items()):
        lines.append(f"; {key} = {value}")
    return "\n".join(lines) + "\n"


def _replace_zip_member(path: Path, member: str, content: bytes) -> None:
    # zipfile has no in-place editing; rebuild into a buffer then overwrite
    buf = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            dst.writestr(info, content if info.filename == member else src.read(info.filename))
    path.write_bytes(buf.getvalue())
