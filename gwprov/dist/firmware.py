"""Download verified Retro-Go release firmware and provision filesystem content."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import zipfile

from .project import DEFAULT_CATALOG_REPO, _fetch_checked, _safe_relpath, resolve_project


def checked_member(archive: zipfile.ZipFile, spec: dict) -> bytes:
    path = _safe_relpath(spec['path']).as_posix()
    entries = [entry for entry in archive.infolist() if entry.filename == path]
    if len(entries) != 1 or entries[0].file_size != spec['bytes']:
        raise ValueError(f'release bundle has missing, duplicate or wrong-sized member: {path}')
    data = archive.read(entries[0])
    if hashlib.sha256(data).hexdigest().lower() != str(spec['sha256']).lower():
        raise ValueError(f'release checksum mismatch: {path}')
    return data


def install_firmware(output: str | Path, *, variant: str = 'flash',
                     repo: str = DEFAULT_CATALOG_REPO, version: str | None = None) -> dict:
    release = resolve_project(repo, version)
    candidates = [build for build in release.manifest.get('builds', [])
                  if build.get('storage') == variant and build.get('bank') == 2]
    if len(candidates) != 1:
        raise ValueError(f'release must publish exactly one {variant} bank-2 firmware build')
    build = candidates[0]
    blob = _fetch_checked(release.manifest_url, build['bundle'])
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        files['firmware/intflash.bin'] = checked_member(archive, build['image'])
        for spec in build.get('content', []):
            path = _safe_relpath(spec.get('install', spec['path']))
            if variant == 'flash':
                partition = 'littlefs' if path.parts[0] in {'cores', 'lang', 'data'} else 'frogfs'
                path = Path(partition) / path
            dest = (Path(variant) / path).as_posix()
            if dest in files:
                raise ValueError(f'duplicate firmware content destination: {dest}')
            files[dest] = checked_member(archive, spec)
    root = Path(output).expanduser().resolve()
    metadata_path = root / '.gwprov-firmware.json'
    old = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    if old and (old.get('repo') != release.repo or old.get('variant') != variant):
        raise ValueError('content root belongs to another firmware or variant; use a separate output root')
    owned = set(old.get('files', []))
    for relative in files:
        dest = root / relative
        if any(parent.is_symlink() for parent in (dest, *dest.parents)):
            raise ValueError(f'refusing a symlink in firmware output path: {dest}')
        if dest.exists() and relative not in owned:
            raise ValueError(f'refusing to overwrite unrelated content: {dest}')
    for relative, data in files.items():
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    metadata = {'repo': release.repo, 'version': release.version['tag'], 'variant': variant,
                'build': build['id'], 'firmware': release.manifest['firmware'],
                'littlefsBlockSize': build.get('littlefsBlockSize', 4096), 'files': list(files)}
    metadata_path.write_text(json.dumps(metadata, indent=2) + '\n')
    return metadata
