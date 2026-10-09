"""Stage an unpublished local project build into a gwprov content tree."""
from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from .project import _plain_name, _safe_relpath


def stage_local_project(manifest_path: str | Path, *, output: str | Path) -> dict[str, Any]:
    """Copy local build artifacts into the selected content tree.

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
    if variant not in {"flash", "sd"}:
        raise ValueError("local project variant must be flash or sd")
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
    firmware_marker = root / ".gwprov-firmware.json"
    if not firmware_marker.is_file():
        raise ValueError(f"content root has no Retro-Go installation marker: {firmware_marker}")
    firmware_metadata = json.loads(firmware_marker.read_text(encoding="utf-8"))
    if firmware_metadata.get("variant") != variant:
        raise ValueError(
            f"local project variant {variant!r} does not match content root variant "
            f"{firmware_metadata.get('variant')!r}")
    project_root = manifest_file.parent
    planned: list[tuple[Path, bytes, dict[str, Any]]] = []
    symbol_files: list[tuple[Path, bytes]] = []
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
        if variant == "flash":
            if len(destination.parts) < 2 or destination.parts[0] not in {"frogfs", "littlefs"}:
                raise ValueError(f"flash destination must start with frogfs/ or littlefs/: {destination}")
            if destination.parts[0] == "littlefs" and destination.parts[1] not in {"cores", "lang", "data"}:
                raise ValueError(f"unsupported LittleFS destination: {destination}")
        elif destination.parts[0] in {"frogfs", "littlefs"}:
            raise ValueError(f"SD destination is relative to the card root; remove {destination.parts[0]!r}: {destination}")
        if variant == "sd" and destination.parts[0] == ".gwprov-projects.json":
            raise ValueError("SD destination conflicts with gwprov's project ownership marker")
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
            mapped_path = (destination.parts[1:]
                           if variant == "flash" and destination.parts[0] == "frogfs"
                           else destination.parts if variant == "sd" else ())
            if (not isinstance(map_info, dict) or not mapped_path or
                    mapped_path[0] not in {"cores", "homebrews"}):
                raise ValueError(
                    "mapped artifacts must be files under FrogFS/cores, "
                    "FrogFS/homebrews, SD cores/, or SD homebrews/"
                )
            relative = _safe_relpath("/".join(mapped_path))
            base = map_info.get("relocBase")
            if isinstance(base, str):
                try:
                    base_value = int(base, 0)
                except ValueError as exc:
                    raise ValueError("mapped relocBase must be an integer") from exc
            elif isinstance(base, int):
                base_value = base
            else:
                raise ValueError("mapped artifact needs relocBase")
            mapped.append({"path": relative.as_posix(), "relocBase": base_value})
        planned.append((destination, data, metadata))

    symbol_entries = target.get("symbols", [])
    if not isinstance(symbol_entries, list):
        raise ValueError("target symbols must be a list")
    identity = _safe_relpath(repo.replace("/", "--"))
    for entry in symbol_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("each target symbol needs a source path")
        source = Path(entry["source"]).expanduser()
        if not source.is_absolute():
            source = project_root / source
        source = source.resolve()
        if not source.is_file():
            raise ValueError(f"local project symbol source is not a file: {source}")
        filename = entry.get("filename", source.name)
        if not isinstance(filename, str):
            raise ValueError(f"unsafe debug symbol filename: {filename!r}")
        filename = _plain_name(filename)
        data = source.read_bytes()
        expected = entry.get("sha256")
        digest = hashlib.sha256(data).hexdigest()
        if expected is not None and str(expected).lower() != digest:
            raise ValueError(f"local project symbol checksum mismatch: {source}")
        if filename.lower().endswith(".elf") and not data.startswith(b"\x7fELF"):
            raise ValueError(f"local project debug symbols are not an ELF file: {source}")
        relative = _safe_relpath(
            f"projects/{identity.as_posix()}/{target['id']}/{filename}")
        if any(path == relative for path, _ in symbol_files):
            raise ValueError(f"duplicate target debug symbol destination: {relative}")
        symbol_files.append((relative, data))

    marker = root / variant / ".gwprov-projects.json"
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
        if variant == "flash" and parts and parts[0] in {"frogfs", "littlefs"}:
            parts = parts[1:]
        return Path(*parts).as_posix().casefold()

    old_files = {content_key(str(item)) for item in existing.get("files", [])}
    old_symbols = {str(item) for item in existing.get("symbols", [])}
    for destination, _data, _meta in planned:
        full = root / variant / destination
        if full.exists() and content_key(destination.as_posix()) not in old_files:
            raise ValueError(f"refusing to overwrite unowned content: {full}")
        if any(part.is_symlink() for part in (full, *full.parents) if part.exists()):
            raise ValueError(f"refusing to follow symlink in install path: {full}")
    for relative, _data in symbol_files:
        full = root / "debug" / relative
        if any(part.is_symlink() for part in (full, *full.parents) if part.exists()):
            raise ValueError(f"refusing to follow symlink in debug symbol path: {full}")
        if full.exists() and relative.as_posix() not in old_symbols:
            raise ValueError(f"refusing to overwrite unowned debug symbols: {full}")

    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{root.name}.gwprov-local-", dir=root.parent) as temp:
        stage = Path(temp)
        for destination, data, _meta in planned:
            staged = stage / variant / destination
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(data)
        for destination, _data, _meta in planned:
            source = stage / variant / destination
            final = root / variant / destination
            final.parent.mkdir(parents=True, exist_ok=True)
            source.replace(final)
        for relative, data in symbol_files:
            source = stage / "debug" / relative
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(data)
            final = root / "debug" / relative
            final.parent.mkdir(parents=True, exist_ok=True)
            source.replace(final)
        files = [destination.as_posix() for destination, _data, _meta in planned]
        keep = {content_key(name) for name in files}
        other_owned = {
            content_key(str(name))
            for key, item in ownership.items() if key not in {existing_key, project_key}
            and isinstance(item, dict)
            for name in item.get("files", [])
        }
        stale: list[Path] = []
        for old_name in existing.get("files", []):
            old_path = Path(str(old_name))
            # Early local manifests recorded flash paths without their partition.
            # Leave those ambiguous legacy files alone; new markers retain the
            # complete path so stale files can be removed safely.
            if variant == "flash" and old_path.parts and old_path.parts[0] not in {"frogfs", "littlefs"}:
                continue
            if content_key(str(old_path)) in keep or content_key(str(old_path)) in other_owned:
                continue
            candidate = root / variant / old_path
            if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.exists()):
                continue
            if candidate.is_file():
                stale.append(candidate)
        for candidate in stale:
            candidate.unlink()
            parent = candidate.parent
            while parent != root / variant:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        new_symbols = {path.as_posix() for path, _ in symbol_files}
        for old_symbol in old_symbols - new_symbols:
            old_path = Path(old_symbol)
            if old_path.is_absolute() or ".." in old_path.parts:
                continue
            candidate = root / "debug" / old_path
            if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.exists()):
                continue
            if candidate.is_file():
                candidate.unlink()
        if existing_key != project_key:
            ownership.pop(existing_key, None)
        ownership[project_key] = {
            "repo": repo, "tag": tag, "project": project,
            "target": target["id"], "variant": variant,
            "files": files, "requiresAbi": requires_abi,
            "mapped": mapped, "inputs": [], "symbols": sorted(new_symbols),
        }
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(ownership, indent=2) + "\n", encoding="utf-8")
    return {"repo": repo, "tag": tag, "target": target["id"],
            "variant": variant, "files": files, "mapped": mapped,
            "symbols": sorted(new_symbols)}
