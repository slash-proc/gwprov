"""Read loose files and single-file ROM ZIPs without extracting to disk."""
from __future__ import annotations

from pathlib import Path
import zipfile

MAX_INPUT_BYTES = 512 * 1024 * 1024


def read_input(path: Path, *, extensions: set[str] | None = None,
               max_bytes: int = MAX_INPUT_BYTES) -> tuple[str, bytes]:
    """Use the inner basename as identity; retain native ZIPs declared by a core."""
    extensions = {ext.casefold() for ext in extensions or ()}
    unpack = path.suffix.casefold() == ".zip" and ".zip" not in extensions
    if not unpack:
        if path.stat().st_size > max_bytes:
            raise ValueError(f"{path.name}: input exceeds {max_bytes} bytes")
        name, data = path.name, path.read_bytes()
    else:
        try:
            with zipfile.ZipFile(path) as archive:
                files = [entry for entry in archive.infolist() if not entry.is_dir()]
                if len(files) != 1:
                    raise ValueError(f"{path.name}: holds {len(files)} files; a ROM archive must hold exactly one")
                entry = files[0]
                if entry.flag_bits & 1:
                    raise ValueError(f"{path.name}: encrypted ZIP entries are unsupported")
                if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise ValueError(f"{path.name}: only stored and deflate ZIP entries are supported")
                if entry.file_size > max_bytes:
                    raise ValueError(f"{path.name}: unpacked input exceeds {max_bytes} bytes")
                name = entry.filename.rsplit("/", 1)[-1]
                if not name:
                    raise ValueError(f"{path.name}: ZIP entry has no filename")
                with archive.open(entry) as stream:
                    data = stream.read(max_bytes + 1)
                if len(data) > max_bytes or len(data) != entry.file_size:
                    raise ValueError(f"{path.name}: invalid unpacked input size")
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
            raise ValueError(f"{path.name}: could not read ZIP: {exc}") from exc
    if extensions and Path(name).suffix.casefold() not in extensions:
        raise ValueError(f"{name}: expected extension from {', '.join(sorted(extensions))}")
    return name, data
