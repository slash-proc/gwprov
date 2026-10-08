"""Stage an unpublished local project build into a gwprov content tree."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .project import _safe_relpath


def stage_local_project(manifest_path: str | Path, *, output: str | Path) -> dict[str, Any]:
    """Copy local build artifacts into flash content and record ownership.

    The local manifest is intentionally explicit: every source file and every
    destination is named, and mapped artifacts carry the same relocation
    metadata consumed by profile creation. Sources are resolved relative to
    the manifest, so a build can stage without publishing a GWRG release.
    """
    manifest_file = Path(manifest_path).expanduser().resolve()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1:
        raise ValueError("local project manifest must use schemaVersion 1")
    repo = manifest.get("repo")
    tag = manifest.get("tag")
    project = manifest.get("project")
    variant = manifest.get("variant")
    target = manifest.get("target")
    if not all(isinstance(x, str) and x for x in (repo, tag, project, variant)):
        raise ValueError("local project manifest needs repo, tag, project and variant")
    if variant != "flash":
        raise ValueError("local project staging currently supports flash content")
    if not isinstance(target, dict) or not isinstance(target.get("id"), str) or not target["id"]:
        raise ValueError("local project manifest needs a target object with an id")
    requires_abi = target.get("requiresAbi")
    if requires_abi is not None and (not isinstance(requires_abi, dict) or
            not isinstance(requires_abi.get("version"), int) or
            not isinstance(requires_abi.get("minSize"), int)):
        raise ValueError("target requiresAbi must contain integer version and minSize")
    entries = target.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("target files must be a non-empty list")

    root = Path(output).expanduser().resolve()
    project_root = manifest_file.parent
    planned: list[tuple[Path, bytes, dict[str, Any]]] = []
    mapped: list[dict[str, Any]] = []
    destinations: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("each target file needs a source path")
        source = Path(entry["source"]).expanduser()
        if not source.is_absolute():
            source = project_root / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError(f"local project source is not a file: {source}")
        destination = _safe_relpath(entry.get("path", ""))
        if len(destination.parts) < 2 or destination.parts[0] not in {"frogfs", "littlefs"}:
            raise ValueError(f"flash destination must start with frogfs/ or littlefs/: {destination}")
        if destination.parts[0] == "littlefs" and destination.parts[1] not in {"cores", "lang", "data"}:
            raise ValueError(f"unsupported LittleFS destination: {destination}")
        key = destination.as_posix().casefold()
        if key in destinations:
            raise ValueError(f"local project destination collision: {destination}")
        destinations.add(key)
        data = source.read_bytes()
        expected = entry.get("sha256")
        digest = hashlib.sha256(data).hexdigest()
        if expected is not None and str(expected).lower() != digest:
            raise ValueError(f"local project source checksum mismatch: {source}")
        map_info = entry.get("mapped")
        metadata: dict[str, Any] = {"path": destination.as_posix(), "sha256": digest}
        if map_info is not None:
            if not isinstance(map_info, dict) or destination.parts[0] != "frogfs" or destination.parts[1] != "cores":
                raise ValueError("mapped artifacts must be FrogFS files under cores/")
            relative = _safe_relpath("/".join(destination.parts[1:]))
            base = map_info.get("relocBase")
            if isinstance(base, str):
                try: base_value = int(base, 0)
                except ValueError as exc: raise ValueError("mapped relocBase must be an integer") from exc
            elif isinstance(base, int):
                base_value = base
            else:
                raise ValueError("mapped artifact needs relocBase")
            mapped.append({"path": relative.as_posix(), "relocBase": base_value})
        planned.append((destination, data, metadata))

    marker = root / "flash/.gwprov-projects.json"
    ownership = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
    if not isinstance(ownership, dict):
        raise ValueError(f"invalid project ownership marker: {marker}")
    project_key = f"{repo}:{target['id']}"
    existing_key = project_key
    existing = ownership.get(existing_key)
    if existing is None:
        # Older local staging markers used repo/target identity under a
        # different key spelling. Recognize that identity when updating.
        existing_key = next((key for key, item in ownership.items()
                             if isinstance(item, dict) and item.get("repo") == repo
                             and item.get("target") == target["id"]), project_key)
        existing = ownership.get(existing_key, {})

    def content_key(value: str) -> str:
        parts = Path(value).parts
        if parts and parts[0] in {"frogfs", "littlefs"}:
            parts = parts[1:]
        return Path(*parts).as_posix().casefold()

    old_files = {content_key(str(item)) for item in existing.get("files", [])}
    for destination, _data, _meta in planned:
        full = root / "flash" / destination
        if full.exists() and content_key(destination.as_posix()) not in old_files:
            raise ValueError(f"refusing to overwrite unowned content: {full}")
        if any(part.is_symlink() for part in (full, *full.parents) if part.exists()):
            raise ValueError(f"refusing to follow symlink in install path: {full}")

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{root.name}.gwprov-local-", dir=root.parent) as temp:
        stage = Path(temp)
        for destination, data, _meta in planned:
            staged = stage / "flash" / destination
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
        for destination, _data, _meta in planned:
            source = stage / "flash" / destination
            final = root / "flash" / destination
            final.parent.mkdir(parents=True, exist_ok=True)
            source.replace(final)
        files = [Path(*destination.parts[1:]).as_posix()
                 for destination, _data, _meta in planned]
        if existing_key != project_key:
            ownership.pop(existing_key, None)
        ownership[project_key] = {
            "repo": repo, "tag": tag, "project": project,
            "target": target["id"], "variant": variant,
            "files": files, "requiresAbi": requires_abi,
            "mapped": mapped, "inputs": [],
        }
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(ownership, indent=2) + "\n", encoding="utf-8")
    return {"repo": repo, "tag": tag, "target": target["id"],
            "variant": variant, "files": files, "mapped": mapped}
