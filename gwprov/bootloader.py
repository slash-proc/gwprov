"""Resolve and validate the bank-1 bootloader from a release or local file."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
import struct
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_REPO = "sylverb/game-and-watch-bootloader"
DEFAULT_VERSION = "v1.0.8"
ASSET = "gnw_bootloader.bin"
SD_ASSET = "gnw_bootloader_0x08032000.bin"
SD_ADDRESS = 0x08032000
SD_REGION_END = 0x08040000
SD_ASSET_SHA256 = "43fd718c1a53dda140f29e4025fc8b6d3bfa71f19a710139135f998e4de022d7"
SD_ASSET_BYTES = 50_248
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _validate_repo(repo: str) -> None:
    if not _REPO_RE.fullmatch(repo) or ".." in repo:
        raise ValueError(f"invalid GitHub repository {repo!r}; expected 'owner/repo'")


def _release_tag(repo: str) -> str:
    request = Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"Accept": "application/vnd.github.v3+json"},
    )
    with urlopen(request) as response:
        return json.load(response)["tag_name"]


def _download_asset(repo: str, tag: str, asset: str, destination: Path) -> Path:
    if destination.is_file() and destination.stat().st_size:
        return destination
    url = (
        f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}/"
        f"{quote(asset, safe='')}"
    )
    try:
        with urlopen(url) as response:
            data = response.read()
    except HTTPError as exc:
        if exc.code == 404:
            raise ValueError(f"release {tag} of {repo} has no asset {asset!r}") from exc
        raise
    if not data:
        raise ValueError(f"downloaded release asset {asset!r} from {repo} {tag} is empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


def resolve_bootloader(
    *,
    cache: Path,
    repo: str = DEFAULT_REPO,
    version: str = DEFAULT_VERSION,
    local: str | Path | None = None,
):
    if local is not None:
        path = Path(local).expanduser().resolve()
        info = {"source": "local", "path": str(path)}
    else:
        _validate_repo(repo)
        tag = _release_tag(repo) if version == "latest" else version
        key = hashlib.sha256(f"{repo}:{tag}".encode()).hexdigest()
        path = _download_asset(repo, tag, ASSET, cache / "bootloader" / key / ASSET)
        info = {"source": "release", "repo": repo, "version": tag, "asset": ASSET}
    if not 8 <= path.stat().st_size <= 256 * 1024:
        raise ValueError("bootloader must fit bank 1 and contain a vector table")
    data = path.read_bytes()
    stack, reset = struct.unpack_from("<II", data)
    if (
        not 0x20000000 <= stack <= 0x20020000
        or not reset & 1
        or not 0x08000000 <= (reset & ~1) < 0x08000000 + len(data)
    ):
        raise ValueError("bootloader vectors do not target the 0x08000000 bank-1 image")
    return data, {
        **info,
        "address": "0x08000000",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def resolve_sd_bootloader(
    *,
    cache: Path,
    repo: str = DEFAULT_REPO,
    version: str = DEFAULT_VERSION,
    local: str | Path | None = None,
):
    """Resolve the SD bootloader used by patched stock firmware's Game+Left path."""
    if local is not None:
        path = Path(local).expanduser().resolve()
        info = {"source": "local", "path": str(path), "version": None}
    elif repo == DEFAULT_REPO and version == DEFAULT_VERSION:
        path = Path(__file__).parent / "vendor" / "firmware" / SD_ASSET
        info = {"source": "vendored", "repo": repo, "version": version, "asset": SD_ASSET}
    else:
        _validate_repo(repo)
        tag = _release_tag(repo) if version == "latest" else version
        key = hashlib.sha256(f"{repo}:{tag}:{SD_ASSET}".encode()).hexdigest()
        path = _download_asset(repo, tag, SD_ASSET, cache / "bootloader" / key / SD_ASSET)
        info = {"source": "release", "repo": repo, "version": tag, "asset": SD_ASSET}
    if not path.is_file():
        raise ValueError(f"SD bootloader is missing: {path}")
    data = path.read_bytes()
    if not 8 <= len(data) <= SD_REGION_END - SD_ADDRESS:
        raise ValueError(
            f"SD bootloader is {len(data):,} bytes; the 0x08032000–0x08040000 "
            f"region holds at most {SD_REGION_END - SD_ADDRESS:,} bytes"
        )
    stack, reset = struct.unpack_from("<II", data)
    reset_address = reset & ~1
    if (
        not 0x20000000 <= stack <= 0x20020000
        or not (reset & 1)
        or not SD_ADDRESS <= reset_address < SD_ADDRESS + len(data)
    ):
        raise ValueError(f"SD bootloader vectors do not target its 0x{SD_ADDRESS:08X} image")
    digest = hashlib.sha256(data).hexdigest()
    if info["source"] == "vendored" and (len(data) != SD_ASSET_BYTES or digest != SD_ASSET_SHA256):
        raise ValueError("vendored SD bootloader failed its pinned size/hash check")
    return data, {**info, "address": f"0x{SD_ADDRESS:08X}", "bytes": len(data), "sha256": digest}
