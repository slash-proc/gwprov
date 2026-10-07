"""Use gnwmanager's release bindings to obtain the bank-1 bootloader."""
from __future__ import annotations
import hashlib
from pathlib import Path
import struct

DEFAULT_REPO = "sylverb/game-and-watch-bootloader"
DEFAULT_VERSION = "v1.0.8"
ASSET = "gnw_bootloader.bin"


def resolve_bootloader(*, cache: Path, repo: str = DEFAULT_REPO,
                       version: str = DEFAULT_VERSION, local: str | Path | None = None):
    if local is not None:
        path = Path(local).expanduser().resolve()
        info = {"source": "local", "path": str(path)}
    else:
        # These are also the bindings used by gnwmanager.get_bootloader().
        # Pass our local cache path rather than changing gnwmanager's global cache.
        from gnwmanager.plugins.fetch import (download_release_asset,
                                             resolve_latest_tag, validate_repo)
        validate_repo(repo)
        tag = resolve_latest_tag(repo) if version == "latest" else version
        key = hashlib.sha256(f"{repo}:{tag}".encode()).hexdigest()
        path = download_release_asset(repo, tag, ASSET, cache / "bootloader" / key / ASSET)
        info = {"source": "release", "repo": repo, "version": tag, "asset": ASSET}
    if not 8 <= path.stat().st_size <= 256 * 1024:
        raise ValueError("bootloader must fit bank 1 and contain a vector table")
    data = path.read_bytes()
    stack, reset = struct.unpack_from("<II", data)
    if not 0x20000000 <= stack <= 0x20020000 or not reset & 1 or not 0x08000000 <= (reset & ~1) < 0x08000000 + len(data):
        raise ValueError("bootloader vectors do not target the 0x08000000 bank-1 image")
    return data, {**info, "address": "0x08000000", "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
