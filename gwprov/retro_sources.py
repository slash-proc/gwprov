"""Per-user Retro-Go release index registry."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from .adapters import _config_path

DEFAULT_SOURCE = "sylverb/game-and-watch-retro-go-sd"


def _path() -> Path:
    return _config_path().with_name("retro-go-sources.json")


def _github_versions_url(repo: str) -> str:
    return f"https://{repo.split('/', 1)[0]}.github.io/{repo.split('/', 1)[1]}/dist/versions.json"


def normalize_source(value: str) -> dict[str, str]:
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Retro-Go versions index must be an HTTPS URL")
        if not parsed.path.endswith("versions.json"):
            raise ValueError("Retro-Go source URL must point to versions.json")
        source_id = f"{parsed.hostname}{parsed.path.rsplit('/', 1)[0]}".strip("/").casefold()
        return {"id": source_id, "repo": source_id, "versionsUrl": value}
    if value.startswith("github/"):
        value = value[len("github/") :]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) or ".." in value:
        raise ValueError(
            "Retro-Go source must be owner/repo, github/owner/repo, or an HTTPS versions.json URL"
        )
    repo = value.casefold()
    return {"id": repo, "repo": repo, "versionsUrl": _github_versions_url(repo)}


def _read() -> dict:
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "schemaVersion": 1,
            "default": DEFAULT_SOURCE,
            "sources": [normalize_source(DEFAULT_SOURCE)],
        }
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"cannot read Retro-Go source registry {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise RuntimeError(f"invalid Retro-Go source registry: {path}")
    return data


def _write(data: dict) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


def list_sources() -> dict:
    data = _read()
    ids = set()
    rows = []
    for source in data["sources"]:
        if not isinstance(source, dict) or not all(
            isinstance(source.get(k), str) for k in ("id", "repo", "versionsUrl")
        ):
            raise RuntimeError("invalid Retro-Go source entry")
        if source["id"] in ids:
            continue
        ids.add(source["id"])
        rows.append(dict(source, default=source["id"] == data.get("default")))
    return {"default": data.get("default", DEFAULT_SOURCE), "sources": rows}


def add_source(value: str) -> dict:
    data = _read()
    source = normalize_source(value)
    if any(item.get("id") == source["id"] for item in data["sources"]):
        raise ValueError(f"Retro-Go source is already registered: {source['id']}")
    data["sources"].append(source)
    _write(data)
    return source


def remove_source(value: str) -> dict:
    data = _read()
    source = normalize_source(value)
    if data.get("default") == source["id"]:
        raise ValueError("cannot remove the default Retro-Go source; choose another default first")
    filtered = [item for item in data["sources"] if item.get("id") != source["id"]]
    if len(filtered) == len(data["sources"]):
        raise ValueError(f"Retro-Go source is not registered: {source['id']}")
    data["sources"] = filtered
    _write(data)
    return source


def set_default(value: str | None = None) -> dict:
    data = _read()
    if value is None:
        return {"default": data.get("default", DEFAULT_SOURCE)}
    source = normalize_source(value)
    existing = next((item for item in data["sources"] if item.get("id") == source["id"]), None)
    if existing is None:
        raise ValueError(f"Retro-Go source is not registered: {source['id']}")
    data["default"] = existing["id"]
    _write(data)
    return {"default": existing["id"]}


def resolve_release(reference: str | None = None) -> tuple[dict[str, str], str]:
    data = _read()
    if reference is None:
        source_id = data.get("default", DEFAULT_SOURCE)
        raise ValueError(
            f"select an exact Retro-Go release from `gwprov retro-go list`; default source is {source_id}"
        )
    try:
        source_id, tag = reference.rsplit(":", 1)
    except ValueError as exc:
        raise ValueError(
            "Retro-Go release must be SOURCE:VERSION from `gwprov retro-go list`"
        ) from exc
    if source_id.startswith("github/"):
        source_id = source_id[len("github/") :]
    source = next(
        (item for item in data["sources"] if item.get("id") == source_id.casefold()), None
    )
    if source is None:
        raise ValueError(f"Retro-Go release source is not registered: {source_id}")
    from .dist.project import list_versions

    index = list_versions(source["repo"], versions_url=source["versionsUrl"])
    if not any(isinstance(item, dict) and item.get("tag") == tag for item in index["versions"]):
        raise ValueError(f"Retro-Go release {reference!r} is not listed by its registered source")
    return source, tag


def list_releases(source_filter: str | None = None) -> list[dict]:
    data = _read()
    sources = data["sources"]
    if source_filter:
        normalized = normalize_source(source_filter)
        sources = [row for row in sources if row.get("id") == normalized["id"]]
        if not sources:
            raise ValueError(f"Retro-Go source is not registered: {normalized['id']}")
    from .dist.project import list_versions

    rows = []
    for source in sources:
        index = list_versions(source["repo"], versions_url=source["versionsUrl"])
        for version in index["versions"]:
            if not isinstance(version, dict) or not isinstance(version.get("tag"), str):
                continue
            rows.append(
                {
                    "source": source["id"],
                    "version": version["tag"],
                    "name": f"{source['id']}:{version['tag']}",
                    "published": version.get("published") or version.get("date") or "",
                }
            )
    return rows
