"""Import GWRG offline project bundles using the web-builder bundle contract."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import zipfile
import zlib
from urllib.parse import unquote, urljoin, urlsplit
from typing import Any

from .project import (MANIFEST_SCHEMA, MAX_JSON_BYTES, MAX_PUBLISHED_BYTES,
                      ResolvedProject, _plain_name, normalize_repo)

_BUNDLE_BASE = "https://bundle.invalid/"


def _safe_entry(name: str) -> None:
    # Mirror apps/web/src/lib/sources/bundle.ts: reject the stored name before
    # any path normalization and reject the entire archive on one unsafe entry.
    if (not name or name.startswith("/") or "\\" in name or ".." in name
            or re.match(r"^[A-Za-z]:", name)
            or any(ord(char) < 32 or ord(char) == 127 for char in name)):
        raise ValueError(f"unsafe project bundle ZIP entry: {name!r}")


def _json_entry(entries: dict[str, bytes], path: str) -> dict[str, Any]:
    data = entries.get(path)
    if data is None:
        raise ValueError(f"project bundle is missing {path}")
    if len(data) > MAX_JSON_BYTES:
        raise ValueError(f"project bundle JSON exceeds {MAX_JSON_BYTES} bytes: {path}")
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in project bundle {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"project bundle {path} must contain a JSON object")
    return value


def import_project_bundle(path: str | Path) -> tuple[ResolvedProject, dict[str, bytes]]:
    """Read, validate and verify a ZIP made from one GWRG ``dist/<tag>/`` directory.

    The returned asset map is keyed by synthetic absolute URL, as in the builder's
    importBundle() result. No archive member is extracted to the filesystem.
    """
    source = Path(path).expanduser().resolve()
    try:
        archive = zipfile.ZipFile(source)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"could not read project bundle ZIP {source}: {exc}") from exc

    with archive:
        entries: dict[str, bytes] = {}
        try:
            for info in archive.infolist():
                original = getattr(info, "orig_filename", info.filename)
                name = original[:-1] if info.is_dir() and original.endswith("/") else original
                _safe_entry(name)
                if info.is_dir():
                    continue
                if name in entries:
                    raise ValueError(f"duplicate project bundle ZIP entry: {name}")
                # Match the builder's sequential inflation. This archive is an
                # importable project source, so every declared release member is
                # subsequently checked, including symbols and converter binaries.
                entries[name] = archive.read(info)
        except (OSError, EOFError, RuntimeError, NotImplementedError,
                zipfile.BadZipFile, zlib.error) as exc:
            raise ValueError(f"could not unpack project bundle ZIP {source}: {exc}") from exc
        if not entries:
            raise ValueError("project bundle ZIP is empty")

        index = _json_entry(entries, "versions.json") if "versions.json" in entries else None
        if index is not None:
            if (index.get("schemaVersion") != 1
                    or any(not isinstance(index.get(key), str)
                           for key in ("project", "title", "repo", "releasesUrl"))
                    or not isinstance(index.get("versions"), list) or not index["versions"]):
                raise ValueError("unsupported or invalid project bundle versions.json")
            version = index["versions"][0]
            if (not isinstance(version, dict)
                    or any(not isinstance(version.get(key), str)
                           for key in ("tag", "manifest", "publishedAt"))
                    or not isinstance(version.get("prerelease"), bool)
                    or not isinstance(version.get("needsUserFiles"), bool)
                    or version.get("kind") not in {"homebrew", "core"}
                    or not isinstance(version.get("requiresAbi"), dict)):
                raise ValueError("project bundle versions.json has no valid newest manifest")
            manifest_path = version["manifest"]
            if (manifest_path.startswith("/") or "\\" in manifest_path
                    or any(ord(char) < 32 or ord(char) == 127 for char in manifest_path)
                    or any(part in {"", ".", ".."} for part in manifest_path.split("/"))):
                raise ValueError("project bundle manifest path is not a safe relative path")
        else:
            manifest_path = "manifest.json"
            version = None

        manifest = _json_entry(entries, manifest_path)
        if manifest.get("schemaVersion") != MANIFEST_SCHEMA:
            raise ValueError(f"unsupported project manifest schemaVersion: {manifest.get('schemaVersion')!r}")
        if (not isinstance(manifest.get("project"), str)
                or not isinstance(manifest.get("title"), str)
                or not isinstance(manifest.get("tools"), list)
                or not isinstance(manifest.get("targets"), list)):
            raise ValueError("project bundle manifest is missing required project fields")
        raw_source = manifest.get("source")
        source_repo = raw_source.get("repo") if isinstance(raw_source, dict) else None
        if not isinstance(source_repo, str):
            raise ValueError("project bundle manifest has no source.repo")
        repo = normalize_repo(source_repo)
        if index is not None:
            index_repo = index.get("repo")
            if not isinstance(index_repo, str) or normalize_repo(index_repo) != repo:
                raise ValueError("project bundle versions.json repo does not match manifest source.repo")
            selected = version
            version_tag = selected.get("tag")
            if not isinstance(version_tag, str) or not version_tag:
                raise ValueError("project bundle newest version has no tag")
        else:
            version_tag = raw_source.get("ref") if isinstance(raw_source.get("ref"), str) else ""
            selected = {"tag": version_tag, "manifest": "manifest.json",
                        "publishedAt": "", "prerelease": False}

        manifest_url = _BUNDLE_BASE + manifest_path
        manifest_dir = manifest_path.rpartition("/")[0]
        if manifest_dir:
            manifest_dir += "/"

        # Match the builder: verify every manifest-named payload, including
        # artifacts for unselected targets, symbols, shipped BIOS/games and tools.
        specs: list[dict[str, Any]] = []
        targets = manifest.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ValueError("project bundle manifest has no targets")
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("project bundle manifest contains an invalid target")
            artifacts = target.get("artifacts")
            if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
                raise ValueError("project bundle target artifacts must be a list of objects")
            specs.extend(artifacts)
            if "symbols" in target:
                symbols = target["symbols"]
                if not isinstance(symbols, list) or any(not isinstance(item, dict) for item in symbols):
                    raise ValueError("project bundle target symbols must be a list of objects")
                specs.extend(symbols)
            systems = target.get("systems", [])
            if isinstance(systems, list):
                for system in systems:
                    if not isinstance(system, dict):
                        continue
                    for field in ("games", "firmware", "bios"):
                        values = system.get(field, [])
                        if isinstance(values, list):
                            specs.extend(item for item in values
                                         if isinstance(item, dict) and isinstance(item.get("url"), str))
        tools = manifest.get("tools")
        if not isinstance(tools, list):
            raise ValueError("project bundle manifest tools must be a list")
        for tool in tools:
            if not isinstance(tool, dict) or not isinstance(tool.get("binary"), dict):
                raise ValueError("project bundle tool has no valid binary declaration")
            binary = dict(tool["binary"])
            binary.setdefault("file", "tool.wasm")
            specs.append(binary)

        assets: dict[str, bytes] = {}
        for spec in specs:
            ref = spec.get("url")
            declared_name = spec.get("filename") or spec.get("file")
            names = ([declared_name] if isinstance(declared_name, str) else
                     declared_name if isinstance(declared_name, list) else [])
            size = spec.get("bytes")
            digest = spec.get("sha256")
            if not isinstance(ref, str) or not names or any(not isinstance(name, str) for name in names):
                raise ValueError("project bundle payload declaration is missing filename or URL")
            for name in names:
                _plain_name(name)
            if (type(size) is not int or size < 0 or size > MAX_PUBLISHED_BYTES
                    or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-fA-F]{64}", digest)):
                raise ValueError(f"invalid size or SHA-256 in project bundle declaration: {names[0]}")
            _plain_name(ref)
            resolved_url = urljoin(manifest_url, ref)
            url_parts = urlsplit(resolved_url)
            member_name = unquote(url_parts.path.rsplit("/", 1)[-1])
            _plain_name(member_name)
            if member_name not in names:
                raise ValueError(f"project bundle URL and filename disagree: {names[0]}")
            zip_path = manifest_dir + member_name
            data = entries.get(zip_path)
            if data is None:
                raise ValueError(f"project bundle is missing manifest file: {zip_path}")
            if len(data) != size:
                raise ValueError(f"project bundle size mismatch for {member_name}: expected {size}, got {len(data)}")
            if hashlib.sha256(data).hexdigest().lower() != digest.lower():
                raise ValueError(f"project bundle SHA-256 mismatch for {member_name}")
            if resolved_url in assets and assets[resolved_url] != data:
                raise ValueError(f"conflicting project bundle declarations for {member_name}")
            assets[resolved_url] = data

    resolved = ResolvedProject(repo, _BUNDLE_BASE + "versions.json", selected,
                               manifest_url, manifest)
    return resolved, assets
