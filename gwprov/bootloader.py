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
    url = (f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}/"
           f"{quote(asset, safe='')}")
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


def resolve_bootloader(*, cache: Path, repo: str = DEFAULT_REPO,
                       version: str = DEFAULT_VERSION, local: str | Path | None = None):
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
    if not 0x20000000 <= stack <= 0x20020000 or not reset & 1 or not 0x08000000 <= (reset & ~1) < 0x08000000 + len(data):
        raise ValueError("bootloader vectors do not target the 0x08000000 bank-1 image")
    return data, {**info, "address": "0x08000000", "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}
