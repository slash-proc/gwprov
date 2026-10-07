"""Resolve and stage GWRG distribution projects."""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .inputs import read_input

VERSIONS_SCHEMA = 1
MANIFEST_SCHEMA = 1
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PUBLISHED_BYTES = 512 * 1024 * 1024
DEFAULT_CATALOG_REPO = "sylverb/game-and-watch-retro-go-sd"


@dataclass(frozen=True)
class ResolvedProject:
    repo: str
    versions_url: str
    version: dict[str, Any]
    manifest_url: str
    manifest: dict[str, Any]


def versions_url_for(repo: str) -> str:
    repo = normalize_repo(repo)
    owner, name = repo.split("/", 1)
    return f"https://{owner}.github.io/{name}/dist/versions.json"


def normalize_repo(value: str) -> str:
    value = value.strip().removesuffix(".git").strip("/")
    if value.startswith("https://"):
        parsed = urlparse(value)
        if parsed.hostname not in {"github.com", "www.github.com"}:
            raise ValueError("project must be a GitHub owner/repo URL or owner/repo")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ValueError("project must be written as owner/repo")
    return value.lower()


def _json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "gwprov/0.1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise ValueError(f"could not fetch {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"could not fetch {url}: {exc}") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError(f"JSON document exceeds {MAX_JSON_BYTES} bytes: {url}")
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON from {url}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"expected a JSON object from {url}")
    return doc


def list_versions(repo: str, *, versions_url: str | None = None) -> dict[str, Any]:
    requested_repo = normalize_repo(repo) if "/" in repo else repo.casefold()
    index_url = versions_url or versions_url_for(repo)
    if urlparse(index_url).scheme != "https":
        raise ValueError("versions.json URL must use HTTPS")
    index = _json(index_url)
    if index.get("schemaVersion") != VERSIONS_SCHEMA:
        raise ValueError(f"unsupported versions.json schemaVersion: {index.get('schemaVersion')!r}")
    actual_repo = index.get("repo")
    if not isinstance(actual_repo, str):
        raise ValueError("versions.json has no valid repository name")
    actual_repo = normalize_repo(actual_repo)
    if versions_url is None and actual_repo != requested_repo:
        raise ValueError(f"versions.json repo does not match requested project {repo}")
    if not isinstance(index.get("versions"), list):
        raise ValueError("versions.json has no valid versions list")
    return index


def resolve_project(repo: str, tag: str | None = None, *, versions_url: str | None = None) -> ResolvedProject:
    requested_repo = normalize_repo(repo) if "/" in repo else repo.casefold()
    index_url = versions_url or versions_url_for(repo)
    if urlparse(index_url).scheme != "https":
        raise ValueError("versions.json URL must use HTTPS")
    index = _json(index_url)
    if index.get("schemaVersion") != VERSIONS_SCHEMA:
        raise ValueError(f"unsupported versions.json schemaVersion: {index.get('schemaVersion')!r}")
    actual_repo = index.get("repo")
    if not isinstance(actual_repo, str):
        raise ValueError("versions.json has no valid repository name")
    actual_repo = normalize_repo(actual_repo)
    if versions_url is None and actual_repo != requested_repo:
        raise ValueError(f"versions.json repo does not match requested project {repo}")
    repo = actual_repo
    versions = index.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError("versions.json has no published versions")
    selected = next((v for v in versions if isinstance(v, dict) and v.get("tag") == tag), None) if tag else versions[0]
    if not isinstance(selected, dict):
        raise ValueError(f"version {tag!r} is not currently published")
    manifest_ref = selected.get("manifest")
    if not isinstance(manifest_ref, str) or not manifest_ref:
        raise ValueError("version entry has no manifest URL")
    from urllib.parse import urljoin
    manifest_url = urljoin(index_url, manifest_ref)
    if urlparse(manifest_url).scheme != "https":
        raise ValueError("manifest URL must use HTTPS")
    manifest = _json(manifest_url)
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise ValueError(f"unsupported manifest schemaVersion: {manifest.get('schemaVersion')!r}")
    source = manifest.get("source")
    source_repo = source.get("repo", "") if isinstance(source, dict) else ""
    if not isinstance(source_repo, str) or source_repo.lower() != repo:
        raise ValueError("manifest source.repo does not match requested project")
    return ResolvedProject(repo, index_url, selected, manifest_url, manifest)


def load_project_catalog(repo: str = DEFAULT_CATALOG_REPO) -> dict[str, Any]:
    """Load the latest firmware-published catalog and verify its release checksum."""
    firmware = resolve_project(repo)
    metadata = firmware.manifest.get("projects")
    if not isinstance(metadata, dict):
        raise ValueError(f"{repo} release does not publish projects.json")
    raw = _fetch_checked(firmware.manifest_url, metadata)
    try:
        catalog = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid projects.json in {repo}: {exc}") from exc
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
        raise ValueError("unsupported projects.json schemaVersion")
    projects = catalog.get("projects")
    if not isinstance(projects, list):
        raise ValueError("projects.json has no valid projects list")
    for item in projects:
        if (not isinstance(item, dict) or not isinstance(item.get("project"), str)
                or not isinstance(item.get("title"), str) or item.get("kind") not in {"core", "homebrew"}
                or not isinstance(item.get("versionsUrl"), str)
                or urlparse(item["versionsUrl"]).scheme != "https"):
            raise ValueError("projects.json contains an invalid project entry")
    return {"schemaVersion": 1, "source": repo, "projects": projects}


def _catalog_entry(name: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    key = name.strip().casefold().removesuffix(".git")
    for item in catalog["projects"]:
        versions = urlparse(item["versionsUrl"])
        segments = versions.path.strip("/").split("/")
        repository = segments[-3] if len(segments) >= 3 and segments[-2:] == ["dist", "versions.json"] else ""
        owner = (versions.hostname or "").split(".", 1)[0]
        aliases = {item["project"].casefold(), repository.casefold(), f"{owner}/{item['project']}".casefold(),
                   f"{owner}/{repository}".casefold()}
        if key in aliases:
            return item
    return None


def _project_reference(name: str) -> tuple[str, str | None]:
    if "/" in name:
        try:
            item = _catalog_entry(name, load_project_catalog())
        except (OSError, RuntimeError, ValueError):
            item = None
        if item:
            return item["project"], item["versionsUrl"]
        return normalize_repo(name), None
    item = _catalog_entry(name, load_project_catalog())
    if item:
        return item["project"], item["versionsUrl"]
    raise ValueError(f"unknown curated project {name!r}; use `gwprov project list` to list names")


def _fetch_checked(base_url: str, entry: dict[str, Any], *, url_key: str = "url") -> bytes:
    filename = entry.get("filename") or entry.get("file")
    ref = entry.get(url_key)
    expected_size = entry.get("bytes")
    expected_hash = entry.get("sha256")
    if (not isinstance(ref, str) or not isinstance(expected_size, int) or expected_size < 0
            or expected_size > MAX_PUBLISHED_BYTES):
        raise ValueError(f"invalid or excessive published file metadata for {filename or ref}")
    if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise ValueError(f"invalid SHA-256 for published file {filename or ref}")
    from urllib.parse import quote, urljoin, urlsplit, urlunsplit
    url = urljoin(base_url, ref)
    parts = urlsplit(url)
    url = urlunsplit(parts._replace(path=quote(parts.path, safe="/%:@!$&'()*+,;=-._~")))
    if urlparse(url).scheme != "https":
        raise ValueError(f"published file URL must use HTTPS: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": "gwprov/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read(expected_size + 1)
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ValueError(f"could not fetch {url}: {exc}") from exc
    if len(data) != expected_size:
        raise ValueError(f"size mismatch for {filename or ref}: expected {expected_size}, got {len(data)}")
    actual = hashlib.sha256(data).hexdigest()
    if actual.lower() != expected_hash.lower():
        raise ValueError(f"SHA-256 mismatch for {filename or ref}")
    return data


def _plain_name(name: str) -> str:
    if (not name or len(name) > 200 or name in {".", ".."} or name[0] in {".", " "}
            or name.endswith((".", " "))
            or any(ord(c) < 32 or ord(c) == 127 or c in '\\/:*?"<>|' for c in name)):
        raise ValueError(f"unsafe install filename: {name!r}")
    return name


def _safe_relpath(value: str) -> Path:
    # Only manifest-controlled POSIX segments may become device paths.
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError(f"unsafe manifest path: {value!r}")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe manifest path: {value!r}")
    return Path(*parts)


def _system_firmware(system: dict[str, Any]) -> list[dict[str, Any]]:
    entries = system.get("firmware", system.get("bios", []))
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError(f"invalid firmware entries for system {system.get('id')!r}")
    return entries


def _firmware_directory(system: dict[str, Any], system_id: str) -> str:
    return system.get("firmwareDir") or system.get("biosDir") or system_id


def _target_dir(target: dict[str, Any], role: str, system: str | None = None) -> Path:
    kind = target.get("kind")
    if role == "artifact":
        return Path("homebrews") if kind == "homebrew" else Path("cores")
    if role == "game":
        if not system:
            systems = target.get("systems", [])
            if len(systems) != 1:
                raise ValueError("a core with multiple systems needs uses[].system for each converter")
            system = systems[0].get("id")
        if not isinstance(system, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", system):
            raise ValueError(f"invalid ROM system id {system!r}")
        return Path("roms") / system
    if role == "data":
        base = Path("homebrews")
        if target.get("kind") == "homebrew" and target.get("dataDir"):
            base /= _safe_relpath(target["dataDir"])
        return base
    raise ValueError(f"unknown install role {role!r}")


def _input_files(tool: dict[str, Any], file_specs: list[str], dir_specs: list[str], *, require_slots: bool) -> tuple[list[dict[str, Any]], list[str]]:
    slots: dict[str, list[Path]] = {}
    for spec in file_specs:
        if "=" not in spec:
            raise ValueError(f"input must be SLOT=FILE: {spec!r}")
        slot, raw = spec.split("=", 1)
        slots.setdefault(slot, []).append(Path(raw).expanduser().resolve())
    for spec in dir_specs:
        if "=" not in spec:
            raise ValueError(f"input directory must be SLOT=DIR: {spec!r}")
        slot, raw = spec.split("=", 1)
        directory = Path(raw).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError(f"not a directory: {directory}")
        # Slots describe a set such as OpenLara's DATA directory; do not recurse into unrelated subfolders.
        files = sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()), key=lambda p: p.name.casefold())
        input_spec = next((item for item in tool.get("inputs", []) if item.get("id") == slot), {})
        extensions = {ext.casefold() for ext in input_spec.get("extensions", [])}
        if extensions:
            files = [path for path in files if path.suffix.casefold() in extensions | {".zip"}]
        matches = []
        accepted_hashes: set[str] = set()
        hashes = {str(item.get("sha1", "")).lower() for item in input_spec.get("variants", [])}
        for path in files:
            try:
                _, payload = read_input(path, extensions=extensions,
                                        max_bytes=int(input_spec.get("maxBytes") or 512 * 1024 * 1024))
            except ValueError as exc:
                print(f"warning: skipping {path.name}: {exc}")
                continue
            digest = hashlib.sha1(payload).hexdigest()
            if hashes and digest not in hashes:
                continue
            if digest in accepted_hashes:
                continue
            accepted_hashes.add(digest)
            matches.append(path)
        slots.setdefault(slot, []).extend(matches)

    warnings: list[str] = []
    prepared: list[dict[str, Any]] = []
    declared = tool.get("inputs", [])
    declared_by_id = {i.get("id"): i for i in declared if isinstance(i, dict)}
    unknown = set(slots) - set(declared_by_id)
    if unknown:
        raise ValueError("unknown input slot(s): " + ", ".join(sorted(unknown)))
    for slot, spec in declared_by_id.items():
        paths = slots.get(slot, [])
        if require_slots and spec.get("required") and not paths:
            raise ValueError(f"required input slot {slot!r} is missing")
        if paths and not spec.get("allowMultiple", False) and len(paths) > 1:
            raise ValueError(f"input slot {slot!r} accepts one file")
        if spec.get("maxCount") is not None and len(paths) > int(spec["maxCount"]):
            raise ValueError(f"input slot {slot!r} exceeds maxCount={spec['maxCount']}")
        for path in paths:
            if not path.is_file():
                raise ValueError(f"input file does not exist: {path}")
            extensions = spec.get("extensions", [])
            max_bytes = int(spec.get("maxBytes", 0))
            name, data = read_input(path, extensions=set(extensions),
                                    max_bytes=max_bytes or 512 * 1024 * 1024)
            name = _plain_name(name)
            if max_bytes and len(data) > max_bytes:
                raise ValueError(f"{name}: {len(data)} bytes exceeds maxBytes={max_bytes}")
            sha1 = hashlib.sha1(data).hexdigest()
            variants = spec.get("variants", [])
            variant = next((v for v in variants if str(v.get("sha1", "")).lower() == sha1), None)
            if variant and variant.get("bytes") is not None and len(data) != int(variant["bytes"]):
                raise ValueError(f"{name}: size does not match recognised variant {variant.get('id', '')!r}")
            if variant is None and spec.get("strict", True):
                raise ValueError(f"{name}: hash is not a recognised variant for input slot {slot!r}")
            if variant is None and variants:
                warnings.append(f"{name}: unrecognised input accepted by slot {slot!r} (strict=false)")
            prepared.append({"slot": slot, "filename": name, "bytes": data, "variant": variant})
    # A tool may declare one required file globally but mark its input slots optional only when uses is optional.
    return prepared, warnings


def _run_wasm(wasm: bytes, tool: dict[str, Any], inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        import wasmtime
    except ImportError as exc:
        raise ValueError("converter execution requires the optional dependency: pip install 'gwprov[dist]'") from exc
    processor = tool.get("processor", {})
    if processor.get("type") != "wasm" or processor.get("version") != 1:
        raise ValueError(f"unsupported processor {processor!r}")
    binary = tool.get("binary", {})
    if len(wasm) != binary.get("bytes") or hashlib.sha256(wasm).hexdigest().lower() != str(binary.get("sha256", "")).lower():
        raise ValueError("converter binary size or SHA-256 mismatch")

    config = wasmtime.Config()
    config.consume_fuel = True
    engine = wasmtime.Engine(config)
    module = wasmtime.Module(engine, wasm)
    if module.imports:
        raise ValueError("converter WASM imports are not allowed")
    abi_signatures = {
        "abi_version": (0, 1), "alloc": (1, 1), "input_clear": (0, 0),
        "input_add": (2, 1), "run": (1, 1), "run_begin": (1, 1), "run_step": (0, 1),
        "stage_count": (0, 1), "stage_index": (0, 1), "stage_name_ptr": (1, 1),
        "stage_name_len": (1, 1), "output_count": (0, 1), "output_name_ptr": (1, 1),
        "output_name_len": (1, 1), "output_ptr": (1, 1), "output_len": (1, 1),
        "error_ptr": (0, 1), "error_len": (0, 1), "warnings_ptr": (0, 1), "warnings_len": (0, 1),
    }
    exported = {e.name: e for e in module.exports}
    required_names = set(abi_signatures) | {"memory"}
    if not required_names <= set(exported):
        raise ValueError("converter is missing ABI exports: " + ", ".join(sorted(required_names - set(exported))))
    extra_functions = [name for name, export in exported.items()
                       if isinstance(export.type, wasmtime.FuncType) and name not in abi_signatures]
    if extra_functions:
        raise ValueError("converter has unexpected function exports: " + ", ".join(sorted(extra_functions)))
    memory_types = [e.type for e in module.exports if e.name == "memory"]
    if len(memory_types) != 1 or not isinstance(memory_types[0], wasmtime.MemoryType):
        raise ValueError("converter must export exactly one linear memory")
    limits = memory_types[0].limits
    max_pages = int(tool.get("limits", {}).get("maxMemoryPages", 0))
    if limits.max is None or limits.max > max_pages or limits.min > max_pages:
        raise ValueError("converter memory must have a declared maximum within maxMemoryPages")
    for name, (params, results) in abi_signatures.items():
        ftype = exported[name].type
        if not isinstance(ftype, wasmtime.FuncType):
            raise ValueError(f"converter export {name!r} is not a function")
        if len(ftype.params) != params or len(ftype.results) != results:
            raise ValueError(f"converter export {name!r} has an invalid ABI signature")
        if any(str(v) != "i32" for v in (*ftype.params, *ftype.results)):
            raise ValueError(f"converter export {name!r} must use i32 values")
    store = wasmtime.Store(engine)
    store.set_fuel(100_000_000_000)
    instance = wasmtime.Instance(store, module, [])
    exports = instance.exports(store)
    names = {e.name for e in module.exports}
    required = {"memory", "abi_version", "alloc", "input_clear", "input_add", "run", "run_begin", "run_step",
                "stage_count", "stage_index", "stage_name_ptr", "stage_name_len", "output_count",
                "output_name_ptr", "output_name_len", "output_ptr", "output_len", "error_ptr", "error_len",
                "warnings_ptr", "warnings_len"}
    if not required <= names:
        raise ValueError("converter is missing ABI exports: " + ", ".join(sorted(required - names)))
    memory = exports["memory"]
    if exports["abi_version"](store) != 1:
        raise ValueError("unsupported converter ABI version")

    def u32(value: int) -> int:
        return value & 0xffffffff

    def read(ptr: int, length: int, cap: int) -> bytes:
        ptr, length = u32(ptr), u32(length)
        if length > cap or ptr + length > memory.data_len(store):
            raise ValueError("converter returned an out-of-bounds or oversized value")
        return bytes(memory.read(store, ptr, ptr + length))

    def fail(code: int) -> None:
        try:
            message = read(exports["error_ptr"](store), exports["error_len"](store), 65536).decode("utf-8", "strict")
        except Exception:
            message = ""
        raise ValueError(f"converter failed ({u32(code)}): {message}".rstrip())

    exports["input_clear"](store)
    for item in inputs:
        payload = item["bytes"]
        ptr = u32(exports["alloc"](store, len(payload)))
        if ptr == 0 and payload:
            raise ValueError(f"converter could not allocate input {item['filename']}")
        if ptr + len(payload) > memory.data_len(store):
            raise ValueError("converter allocated input outside its memory")
        memory.write(store, payload, ptr)
        exports["input_add"](store, ptr, len(payload))
    code = u32(exports["run_begin"](store, 0))
    if code:
        fail(code)
    for _ in range(1_000_000):
        code = u32(exports["run_step"](store))
        if code == 0:
            break
        if code != 1:
            fail(code)
    else:
        raise ValueError("converter exceeded the run_step limit")

    specs = {o["id"]: o for o in tool.get("outputs", [])}
    count = u32(exports["output_count"](store))
    if count > 256:
        raise ValueError("converter returned too many outputs")
    total_limit = int(tool.get("limits", {}).get("maxOutputBytes", 0))
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i in range(count):
        oid = read(exports["output_name_ptr"](store, i), exports["output_name_len"](store, i), 255).decode("utf-8", "strict")
        spec = specs.get(oid)
        if spec is None:
            matches = [output for output in specs.values() if output.get("filename") == oid]
            spec = matches[0] if len(matches) == 1 else None
        if spec is None or spec["id"] in seen:
            raise ValueError(f"converter returned unknown or duplicate output id {oid!r}")
        oid = spec["id"]
        seen.add(oid)
        size = u32(exports["output_len"](store, i))
        ceiling = min(total_limit, int(spec.get("maxBytes", total_limit)))
        if size > ceiling:
            raise ValueError(f"converter output {oid!r} exceeds {ceiling} bytes")
        payload = read(exports["output_ptr"](store, i), size, ceiling)
        results.append({"output_id": oid, "bytes": payload})
    warning_blob = read(exports["warnings_ptr"](store), exports["warnings_len"](store), 65536)
    try:
        warnings = warning_blob.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError:
        warnings = []
    for warning in warnings:
        print(f"warning: converter: {warning}")
    return results


def install_project(
    repo: str,
    *,
    output: str | Path,
    variant: str,
    versions_url: str | None = None,
    target_id: str | None = None,
    tag: str | None = None,
    input_files: list[str] | None = None,
    input_dirs: list[str] | None = None,
    dry_run: bool = False,
    firmware_files: list[str] | None = None,
    firmware_dirs: list[str] | None = None,
    bios_files: list[str] | None = None,
    bios_dirs: list[str] | None = None,
    game_files: list[str] | None = None,
    game_dirs: list[str] | None = None,
) -> dict[str, Any]:
    if variant not in {"flash", "sd"}:
        raise ValueError("variant must be 'flash' or 'sd'")
    if firmware_files and bios_files or firmware_dirs and bios_dirs:
        raise ValueError("use either firmware arguments or their BIOS aliases, not both")
    bios_files = firmware_files if firmware_files is not None else bios_files
    bios_dirs = firmware_dirs if firmware_dirs is not None else bios_dirs
    resolved = resolve_project(repo, tag, versions_url=versions_url)
    manifest = resolved.manifest
    storage = manifest.get("storage")
    if storage and variant not in storage:
        raise ValueError(f"project does not publish a {variant} variant (supports: {', '.join(storage)})")
    targets = manifest.get("targets")
    if not isinstance(targets, list) or not targets:
        raise ValueError("manifest has no targets")
    if target_id:
        target = next((t for t in targets if isinstance(t, dict) and t.get("id") == target_id), None)
    elif len(targets) == 1:
        target = targets[0]
    else:
        raise ValueError("project has multiple targets; select one with --target")
    if not isinstance(target, dict):
        raise ValueError(f"target {target_id!r} is not published")
    kind = target.get("kind")
    if kind not in {"homebrew", "core"}:
        raise ValueError(f"unsupported project target kind {kind!r}")

    root = Path(output).expanduser().resolve()
    planned: list[tuple[Path, bytes, str]] = []
    collisions: set[str] = set()
    mapped_artifacts: list[dict[str, Any]] = []
    converter_inputs: list[dict[str, Any]] = []

    def add(relative: Path, data: bytes, origin: str, *, allow_identical: bool = False, mapped: bool = False) -> None:
        if variant == "flash":
            partition = "littlefs" if relative.parts[0] in {"cores", "lang", "data"} and not mapped else "frogfs"
            relative = Path(partition) / relative
        relative = Path(variant) / relative
        key = relative.as_posix().casefold()
        for old_path, old_data, old_origin in planned:
            if (Path(variant) / old_path).as_posix().casefold() == key:
                if allow_identical and old_data == data:
                    return
                raise ValueError(f"install path collision at {relative}: {old_origin} and {origin}")
        if key in collisions:
            raise ValueError(f"install path collision at {relative}")
        collisions.add(key)
        planned.append((relative.relative_to(variant), data, origin))

    base = resolved.manifest_url
    for artifact in target.get("artifacts", []):
        filename = _plain_name(artifact.get("filename", ""))
        data = _fetch_checked(base, artifact)
        is_mapped = artifact.get("mapped") is True
        add(_target_dir(target, "artifact") / filename, data, f"artifact:{filename}", mapped=is_mapped)
        if is_mapped:
            mapped_artifacts.append({"path": (_target_dir(target, "artifact") / filename).as_posix(),
                                     "relocBase": artifact.get("relocBase")})
    # Shipped system games and firmware are declarations in the manifest, not core artifacts.
    systems = target.get("systems", []) if kind == "core" else []
    systems_by_id = {system.get("id"): system for system in systems if isinstance(system, dict)}
    selected_games: dict[str, list[tuple[str, bytes, str]]] = {sid: [] for sid in systems_by_id}
    for system in systems:
        sid = system.get("id")
        for game in system.get("games", []):
            filename = _plain_name(game.get("filename", ""))
            data = _fetch_checked(base, game)
            add(_target_dir(target, "game", sid) / filename, data, f"game:{sid}/{filename}")
            selected_games.setdefault(sid, []).append((filename, data, f"shipped:{filename}"))

    # User games are copied as files and also determine conditional firmware requirements.
    game_inputs: dict[str, list[Path]] = {sid: [] for sid in systems_by_id}
    directory_games: set[Path] = set()
    for spec in game_files or []:
        if "=" not in spec:
            raise ValueError(f"game must be SYSTEM=FILE: {spec!r}")
        sid, raw = spec.split("=", 1)
        if sid not in systems_by_id:
            raise ValueError(f"unknown core system {sid!r} in --game")
        game_inputs[sid].append(Path(raw).expanduser().resolve())
    for spec in game_dirs or []:
        if "=" not in spec:
            raise ValueError(f"game directory must be SYSTEM=DIR: {spec!r}")
        sid, raw = spec.split("=", 1)
        if sid not in systems_by_id:
            raise ValueError(f"unknown core system {sid!r} in --game-dir")
        directory = Path(raw).expanduser().resolve()
        if not directory.is_dir():
            raise ValueError(f"not a game directory: {directory}")
        candidates = sorted((p for p in directory.iterdir() if p.is_file() and not p.is_symlink()), key=lambda p: p.name.casefold())
        game_inputs[sid].extend(candidates)
        directory_games.update(candidates)

    for sid, paths in game_inputs.items():
        system = systems_by_id[sid]
        allowed = {ext.casefold() for item in system.get("extensions", [])
                   for ext in (item if isinstance(item, list) else [item]) if isinstance(ext, str)}
        for path in paths:
            if not path.is_file():
                raise ValueError(f"game file does not exist: {path}")
            if path in directory_games and allowed and path.suffix.casefold() not in allowed | {".zip"}:
                continue
            try:
                filename, data = read_input(path, extensions=allowed)
            except ValueError as exc:
                if path not in directory_games:
                    raise
                print(f"warning: skipping {path.name}: {exc}")
                continue
            filename = _plain_name(filename)
            add(_target_dir(target, "game", sid) / filename, data,
                f"user-game:{sid}/{filename}", allow_identical=True)
            selected_games.setdefault(sid, []).append((filename, data, f"user:{filename}"))

    # Firmware inputs can be supplied by id or found by their declared filename in a directory.
    supplied_bios: dict[str, list[Path]] = {}
    for spec in bios_files or []:
        if "=" not in spec:
            raise ValueError(f"firmware input must be ID=FILE: {spec!r}")
        slot, raw = spec.split("=", 1)
        supplied_bios.setdefault(slot, []).append(Path(raw).expanduser().resolve())
    bios_dir_paths = [Path(raw).expanduser().resolve() for raw in (bios_dirs or [])]
    for directory in bios_dir_paths:
        if not directory.is_dir():
            raise ValueError(f"not a firmware directory: {directory}")
    bios_entries = [(sid, system, firmware) for sid, system in systems_by_id.items()
                    for firmware in _system_firmware(system)]
    bios_id_counts: dict[str, int] = {}
    for _, _, bios in bios_entries:
        if isinstance(bios.get("id"), str):
            bios_id_counts[bios["id"]] = bios_id_counts.get(bios["id"], 0) + 1
    bios_slots: dict[str, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for sid, system, bios in bios_entries:
        slot = bios.get("id")
        if not isinstance(slot, str):
            raise ValueError(f"invalid firmware slot in system {sid}")
        key = f"{sid}.{slot}" if bios_id_counts.get(slot, 0) > 1 else slot
        bios_slots[key] = (sid, system, bios)
        # Shipped firmware entries are always downloaded and SHA-256 verified.
        if bios.get("url"):
            canonical = bios.get("filename")
            filename = canonical if isinstance(canonical, str) else canonical[0]
            filename = _plain_name(filename)
            data = _fetch_checked(base, {**bios, "filename": filename})
            bios_key = _firmware_directory(system, sid)
            add(Path("bios") / _safe_relpath(bios_key) / filename, data,
                f"shipped-firmware:{sid}/{filename}", allow_identical=True)

    unknown_bios = set(supplied_bios) - set(bios_slots)
    if unknown_bios:
        raise ValueError("unknown firmware slot(s): " + ", ".join(sorted(unknown_bios)))
    bios_candidates: dict[str, Path] = {}
    for slot, paths in supplied_bios.items():
        if len(paths) != 1:
            raise ValueError(f"firmware slot {slot!r} accepts one file")
        bios_candidates[slot] = paths[0]
    for key, (sid, _, bios) in bios_slots.items():
        names = bios.get("filename")
        names = [names] if isinstance(names, str) else names if isinstance(names, list) else []
        names = [_plain_name(name) for name in names]
        if key not in bios_candidates:
            for directory in bios_dir_paths:
                found = next((directory / name for name in names if (directory / name).is_file()), None)
                if found:
                    bios_candidates[key] = found
                    break
        if bios.get("url"):
            continue
        conditional = {str(ext).casefold() for ext in bios.get("requiredFor", [])}
        game_exts = {Path(name).suffix.casefold() for name, _, _ in selected_games.get(sid, [])}
        needed = bios.get("required") is True or bool(conditional & game_exts)
        candidate = bios_candidates.get(key)
        if not candidate:
            if needed:
                reason = "required" if bios.get("required") else "required for the selected game type"
                raise ValueError(f"missing {reason} firmware {key!r}; pass --firmware {key}=FILE or --firmware-dir DIR")
            continue
        if not candidate.is_file():
            raise ValueError(f"firmware file does not exist: {candidate}")
        data = candidate.read_bytes()
        filename = _plain_name(candidate.name)
        if names and filename.casefold() not in {name.casefold() for name in names}:
            raise ValueError(f"{filename}: filename does not match firmware slot {key!r}")
        if bios.get("bytes") is not None and len(data) != int(bios["bytes"]):
            raise ValueError(f"{filename}: expected {bios['bytes']} bytes, got {len(data)}")
        expected_sha1 = bios.get("sha1")
        if expected_sha1:
            actual_sha1 = hashlib.sha1(data).hexdigest()
            if actual_sha1.lower() != str(expected_sha1).lower():
                message = f"{filename}: firmware SHA-1 does not match slot {key!r}"
                if bios.get("strict", True):
                    raise ValueError(message)
                print(f"warning: {message} (strict=false)")
        bios_key = _firmware_directory(system, sid)
        add(Path("bios") / _safe_relpath(bios_key) / filename, data,
            f"user-firmware:{key}/{filename}", allow_identical=True)

    tools = {t.get("id"): t for t in manifest.get("tools", []) if isinstance(t, dict)}
    uses = target.get("uses", [])
    uses_by_tool = [use for use in uses if isinstance(use, dict)]
    for use in uses_by_tool:
        tool_id = use.get("tool")
        tool = tools.get(tool_id)
        if not tool:
            raise ValueError(f"target references missing tool {tool_id!r}")
        def scoped(specs: list[str] | None, *, allow_bare_dir: bool = False) -> list[str]:
            result: list[str] = []
            input_slots = tool.get("inputs", [])
            for item in specs or []:
                if "=" not in item:
                    if allow_bare_dir and len(uses_by_tool) == 1 and len(input_slots) == 1:
                        result.append(f"{input_slots[0].get('id')}={item}")
                        continue
                    if allow_bare_dir and len(uses_by_tool) > 1:
                        raise ValueError("targets with multiple converters require TOOL.SLOT=DIR syntax")
                    raise ValueError("converter inputs must use SLOT=FILE or SLOT=DIR syntax; a bare directory is accepted only for a single-slot converter")
                key, value = item.split("=", 1)
                if "." in key:
                    prefix, slot = key.split(".", 1)
                    if prefix in tools:
                        if prefix == tool_id:
                            result.append(f"{slot}={value}")
                        continue
                if len(uses_by_tool) > 1:
                    raise ValueError("targets with multiple converters require TOOL.SLOT=FILE syntax")
                result.append(item)
            return result
        provided_files = scoped(input_files)
        provided_dirs = scoped(input_dirs, allow_bare_dir=True)
        prepared, warnings = _input_files(tool, provided_files, provided_dirs, require_slots=bool(use.get("required", True)))
        for warning in warnings:
            print(f"warning: {warning}")
        converter_inputs.extend({"tool": tool_id, "slot": item["slot"],
                                 "filename": item["filename"],
                                 "sha1": hashlib.sha1(item["bytes"]).hexdigest(),
                                 "variant": (item.get("variant") or {}).get("id")}
                                for item in prepared)
        if not prepared:
            if use.get("required", True):
                required_slots = [s.get("id") for s in tool.get("inputs", []) if s.get("required")]
                if required_slots:
                    raise ValueError(f"tool {tool_id!r} is required and needs input slot(s): {', '.join(required_slots)}")
            continue
        wasm = _fetch_checked(base, tool.get("binary", {}), url_key="url")
        groups = [[item for item in prepared if item["slot"] in {i.get("id") for i in tool.get("inputs", [])}]]
        # runPerFile applies per declared slot. For current schemas each runPerFile tool has one slot.
        per_file_slots = {i.get("id") for i in tool.get("inputs", []) if i.get("runPerFile")}
        if per_file_slots:
            other = [item for item in prepared if item["slot"] not in per_file_slots]
            groups = [[item] + other for item in prepared if item["slot"] in per_file_slots]
        outputs: list[tuple[dict[str, Any], bytes, dict[str, Any]]] = []
        produced_ids: set[str] = set()
        for group in groups:
            for converted in _run_wasm(wasm, tool, group):
                produced_ids.add(converted["output_id"])
                input_item = group[0] if group else {"filename": "output", "variant": None}
                spec = next(o for o in tool.get("outputs", []) if o["id"] == converted["output_id"])
                if spec.get("filename"):
                    filename = _plain_name(spec["filename"])
                else:
                    variant_spec = input_item.get("variant") or {}
                    filename = variant_spec.get("filename") or Path(input_item["filename"]).stem + spec["extension"]
                    filename = _plain_name(filename)
                outputs.append((spec, converted["bytes"], {**input_item, "filename": filename}))
        use_outputs = set(use.get("outputs", []))
        missing_outputs = use_outputs - produced_ids
        if missing_outputs:
            raise ValueError(f"converter {tool_id!r} did not produce declared output(s): {', '.join(sorted(missing_outputs))}")
        for spec, data, input_item in outputs:
            if spec["id"] not in use_outputs:
                continue
            if kind == "homebrew":
                destination = _target_dir(target, "data")
                if spec.get("subdir"):
                    destination /= _safe_relpath(spec["subdir"])
            else:
                destination = _target_dir(target, "game", use.get("system"))
            output_path = destination / input_item["filename"]
            variant_id = (input_item.get("variant") or {}).get("id")
            existing = next((entry for entry in planned if entry[0].as_posix().casefold() == output_path.as_posix().casefold()), None)
            shipped = next((game for system in systems for game in system.get("games", [])
                            if (system.get("id") == use.get("system") or len(systems) == 1)
                            and str(game.get("filename", "")).casefold() == input_item["filename"].casefold()), None)
            if existing and shipped and variant_id and variant_id.casefold() in input_item["filename"].casefold():
                # The manifest and known input variant name the same game. Keep the shipped,
                # release-verified bytes as the single install item.
                continue
            add(output_path, data, f"converter:{tool_id}/{input_item['filename']}")

    # When a recognized variant resolves to the same filename as a shipped game, keep the
    # release-verified shipped bytes and represent the game only once in the staging plan.
    if dry_run:
        return {"repo": resolved.repo, "tag": resolved.version.get("tag"), "target": target.get("id"),
                "variant": variant, "files": [p.as_posix() for p, _, _ in planned], "dry_run": True}

    # Refuse to overwrite unrelated files. A prior gwprov-owned project install may be upgraded in place.
    marker = root / variant / ".gwprov-projects.json"
    ownership = json.loads(marker.read_text()) if marker.is_file() else {}
    project_key = f"{resolved.repo}:{target['id']}"
    old = ownership.get(project_key, {})
    prior_files = {str(name).casefold() for name in old.get("files", [])}
    for relative, _, origin in planned:
        destination = root / variant / relative
        cursor = root
        for part in (variant, *relative.parts):
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"refusing to follow symlink in install path: {cursor}")
        if destination.exists() and relative.as_posix().casefold() not in prior_files:
            raise ValueError(f"refusing to overwrite existing file not owned by this project: {destination} ({origin})")

    # Stage beside the destination, then move only the planned files into place.
    root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{root.name}.gwprov-", dir=root.parent) as temp_dir:
        stage = Path(temp_dir)
        for relative, data, _ in planned:
            path = stage / variant / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for relative, _, _ in planned:
            source = stage / variant / relative
            destination = root / variant / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
        metadata = {"repo": resolved.repo, "tag": resolved.version.get("tag"),
                    "project": manifest.get("project"), "target": target.get("id"),
                    "variant": variant, "files": [p.as_posix() for p, _, _ in planned],
                    "requiresAbi": target.get("requiresAbi"), "mapped": mapped_artifacts,
                    "inputs": converter_inputs}
        ownership[project_key] = metadata
        root.mkdir(parents=True, exist_ok=True)
        metadata_path = marker
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(ownership, indent=2) + "\n", encoding="utf-8")
    return {"repo": resolved.repo, "tag": resolved.version.get("tag"), "target": target.get("id"),
            "variant": variant, "files": [p.as_posix() for p, _, _ in planned], "dry_run": False}
