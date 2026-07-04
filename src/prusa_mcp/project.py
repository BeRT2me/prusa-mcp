"""Read and write PrusaSlicer .3mf project files."""

import io
import os
import tempfile
import zipfile
from pathlib import Path

_PRINT_CONFIG = "Metadata/Slic3r_PE.config"
_THUMBNAIL = "Metadata/thumbnail.png"


def read_config(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        if _PRINT_CONFIG not in z.namelist():
            msg = f"No {_PRINT_CONFIG} in {path.name} — not a PrusaSlicer project .3mf?"
            raise ValueError(msg)
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
    replaced = False
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            if info.filename == member:
                dst.writestr(info, content)
                replaced = True
            else:
                dst.writestr(info, src.read(info.filename))
        if not replaced:
            dst.writestr(member, content)

    # Atomic replace — a crash mid-write must not corrupt the user's project
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".3mf.tmp")
    with os.fdopen(fd, "wb") as f:
        f.write(buf.getvalue())
    Path(tmp).replace(path)
