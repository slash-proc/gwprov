"""Filesystem operations against a physical device's RAM flasher."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil


def _target_path(value: str) -> str:
    raw = value.replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if ".." in parts or "\0" in raw:
        raise ValueError(f"invalid target filesystem path: {value!r}")
    return (
        "/"
        if raw in ("", "/")
        else "/" + "/".join(part for part in parts if part not in ("/", "."))
    )


def _files(source: str, destination: str):
    root = Path(source).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"media source does not exist: {root}")
    dest = PurePosixPath(_target_path(destination))
    dirs = []
    current = PurePosixPath("/")
    for component in dest.parts[1:]:
        current /= component
        dirs.append(current.as_posix())
    if root.is_file():
        return [(root, (dest / root.name).as_posix())], dirs
    if not root.is_dir():
        raise ValueError(f"media source is not a regular file or directory: {root}")
    files = []
    tree_root = dest / root.name
    for current, names, filenames in os.walk(root):
        current = Path(current)
        for name in names:
            if (current / name).is_symlink():
                raise ValueError(f"media source contains a symlinked directory: {current / name}")
        relative = current.relative_to(root)
        remote = tree_root / PurePosixPath(relative.as_posix()) if relative.parts else tree_root
        dirs.append(remote.as_posix())
        names.sort()
        for name in sorted(filenames):
            local = current / name
            if local.is_symlink() or not local.is_file():
                raise ValueError(f"media source contains a non-regular file: {local}")
            files.append((local, (remote / name).as_posix()))
    return files, sorted(set(dirs), key=lambda value: (value.count("/"), value))


def _backend(row):
    from .backends import SelectedPyOCDBackend, WebSocketBackend

    device_id = row["id"]
    if device_id.startswith("probe:"):
        return SelectedPyOCDBackend(
            device_id.removeprefix("probe:"),
            probe_adapter=row.get("adapter"),
            operation="gwprov device media",
        )
    if device_id.startswith("remote:"):
        return WebSocketBackend(
            device_id.removeprefix("remote:"),
            origin=row.get("remoteOrigin"),
            operation="gwprov device media",
        )
    raise ValueError("media file changes currently require a physical hardware device")


def _read_memory(backend, address: int, size: int) -> bytes:
    parts = []
    while size:
        count = min(size, 60 * 1024)
        chunk = backend.read_memory(address, count)
        if len(chunk) != count:
            raise RuntimeError(
                f"short device read at 0x{address:08x}: expected {count}, got {len(chunk)}"
            )
        parts.append(chunk)
        address += count
        size -= count
    return b"".join(parts)


def _ready_flasher(backend, gnw, *, yes: bool = False):
    from .hw_lifecycle import (
        assert_application_mode,
        attach_to_idle_gnwmanager,
        inspect_target_mode,
        mark_backend_flasher_ready,
        mark_backend_recovery_required,
        start_gnwmanager_verified,
    )

    state = inspect_target_mode(backend)
    if state["stubDetected"]:
        if not state["stubAlive"]:
            raise RuntimeError(
                "gnwmanager is resident but not live and idle; no media command was sent"
            )
        mark_backend_recovery_required(backend, "synchronizing gnwmanager for media operation")
        attach_to_idle_gnwmanager(backend, gnw)
    else:
        assert_application_mode(backend, operation="edit device media")
        if not yes:
            from rich.prompt import Confirm

            if not Confirm.ask(
                "The recovery flasher is not running. Start it to edit device media?", default=False
            ):
                raise ValueError(
                    "media edit cancelled; rerun with --yes to start the recovery flasher automatically"
                )
        mark_backend_recovery_required(backend, "starting gnwmanager for media operation")
        start_gnwmanager_verified(backend, gnw)
        mark_backend_recovery_required(backend, "gnwmanager media operation")
    return mark_backend_flasher_ready


def _device_frogfs(
    backend,
    gnw,
    row,
    *,
    operation: str,
    path: str,
    source: str | None,
    contiguous: bool = False,
    sentinel: int | None = None,
) -> int:
    """Rebuild live FrogFS using its assigned profile's layout and XiP metadata."""
    import json
    import struct
    import tempfile
    import zlib
    from .deploy import inspect_device_extflash
    from .filesystem import _read_frogfs, operate
    from .profiles import DeviceProfile

    from .device_assignments import get_assignment

    assigned = get_assignment(row["id"]).get("profile") or row.get("assignedProfile")
    if not assigned:
        raise ValueError(
            "device FrogFS edits need an assigned profile for its layout and mapped XiP metadata; "
            "apply the source profile to this device first"
        )
    profile = DeviceProfile.load(assigned)
    metadata_path = profile.root / "provision.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    layout = metadata.get("layout", {})
    frog_offset = int(layout.get("frogfsOffset", -1))
    frog_size = int(layout.get("frogfsBytes", 0))
    lfs_offset = int(layout.get("littlefsOffset", -1))
    if frog_offset < 0 or frog_size <= 0 or lfs_offset <= frog_offset + frog_size:
        raise ValueError("assigned profile has no valid FrogFS/LittleFS layout")
    live = inspect_device_extflash(backend, gnw)
    live_frog = next(
        (item for item in live["regions"] if item["content"].startswith("FrogFS")), None
    )
    live_lfs = next(
        (item for item in live["regions"] if item["content"].startswith("LittleFS")), None
    )
    if (
        not live_frog
        or int(live_frog["offset"]) != frog_offset
        or int(live_frog["bytes"]) != frog_size
        or not live_lfs
        or int(live_lfs["offset"]) != lfs_offset
    ):
        raise ValueError(
            "assigned profile filesystem layout does not match the device's live firmware layout"
        )
    if live["size"] < lfs_offset:
        raise ValueError(
            "assigned profile filesystem layout exceeds the device's extflash capacity"
        )

    flash = bytearray(_read_memory(backend, 0x08100000, 256 * 1024))
    layout_hits = []
    for pos in range(0, len(flash) - 35, 4):
        if (
            flash[pos : pos + 8] == b"GWLB\x02\x00\x24\x00"
            and zlib.crc32(flash[pos : pos + 32]) == struct.unpack_from("<I", flash, pos + 32)[0]
        ):
            layout_hits.append(pos)
    if len(layout_hits) != 1:
        raise ValueError("device bank 2 has no unique valid Retro-Go filesystem layout")
    current_ext = bytearray(b"\xff" * live["size"])
    actual_region = _read_memory(backend, 0x90000000 + frog_offset, lfs_offset - frog_offset)
    current_ext[frog_offset:lfs_offset] = actual_region

    with tempfile.TemporaryDirectory(prefix="gwprov-device-frogfs-") as temp_dir:
        root = Path(temp_dir)
        (root / "bank1.bin").write_bytes(b"\xff" * (256 * 1024))
        (root / "bank2.bin").write_bytes(flash)
        (root / "extflash.bin").write_bytes(current_ext)
        (root / "profile.toml").write_text(
            "[version]\n"
            'display_name = "device-live"\n'
            "[flash]\n"
            'bank1 = "bank1.bin"\n'
            'bank2 = "bank2.bin"\n'
            'extflash = "extflash.bin"\n'
            '[sd]\nmode = "none"\n',
            encoding="utf-8",
        )
        metadata["layout"] = dict(layout)
        (root / "provision.json").write_text(json.dumps(metadata, indent=2) + "\n")
        map_path = profile.root / "flash-map.yaml"
        if map_path.is_file():
            shutil.copyfile(map_path, root / "flash-map.yaml")
        for project in metadata.get("projects", {}).values():
            if not isinstance(project, dict):
                continue
            for mapped in project.get("mapped", []):
                raw_source = mapped.get("source") if isinstance(mapped, dict) else None
                if not raw_source:
                    continue
                local_source = (profile.root / raw_source).resolve()
                if profile.root.resolve() not in local_source.parents or not local_source.is_file():
                    raise ValueError(
                        f"mapped XiP source is missing from assigned profile: {raw_source}"
                    )
                staged_source = root / raw_source
                staged_source.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_source, staged_source)
        for item in metadata.get("mediaSentinels", []):
            if not isinstance(item, dict) or not item.get("source"):
                continue
            raw_source = Path(str(item["source"]))
            local_source = (profile.root / raw_source).resolve()
            if (
                raw_source.is_absolute()
                or ".." in raw_source.parts
                or profile.root.resolve() not in local_source.parents
                or not local_source.is_file()
            ):
                raise ValueError(
                    f"sentinel source is missing or invalid in assigned profile: {raw_source}"
                )
            staged_source = root / raw_source
            staged_source.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(local_source, staged_source)
        staged = root / "frogfs-tree"
        staged.mkdir()
        _read_frogfs(root / "extflash.bin", frog_offset, staged)
        project_records = metadata.get("projects", {})
        mapped_sources = {}
        if project_records:
            from .cli.main import _retain_profile_mapped_sources

            mapped_sources = _retain_profile_mapped_sources(root, staged, {}, project_records)
        mapped_args = [f"{name}={filename}" for name, filename in mapped_sources.items()]
        operate(
            operation=operation,
            target="flash/ext",
            profile=str(root),
            filesystem="frogfs",
            path=path,
            source=source,
            mapped_sources=mapped_args,
            contiguous=contiguous,
            sentinel=sentinel,
        )
        rewritten = (root / "extflash.bin").read_bytes()
        capacity = lfs_offset - frog_offset
        gnw.flash(
            0, frog_offset, rewritten[frog_offset : frog_offset + capacity], desc="Writing FrogFS"
        )
        gnw.flash(
            2, 0, (root / "bank2.bin").read_bytes(), desc="Updating firmware filesystem layout"
        )
        staged_metadata = root / "provision.json"
        if staged_metadata.is_file():
            updated_metadata = json.loads(staged_metadata.read_text())
            for item in updated_metadata.get("mediaSentinels", []):
                raw_source = Path(str(item.get("source", "")))
                if raw_source.is_absolute() or ".." in raw_source.parts or not raw_source.parts:
                    raise ValueError(
                        f"invalid sentinel source path in staged profile: {raw_source}"
                    )
                staged_source = root / raw_source
                if not staged_source.is_file():
                    raise ValueError(f"staged sentinel source is missing: {staged_source}")
                destination = profile.root / raw_source
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged_source, destination)
            (profile.root / "provision.json").write_text(
                json.dumps(updated_metadata, indent=2) + "\n"
            )
    print("Updated device FrogFS")
    return 0


def operate_device(
    *,
    device: str,
    operation: str,
    filesystem: str,
    path: str = "/",
    source: str | None = None,
    contiguous: bool = False,
    sentinel: int | None = None,
    yes: bool = False,
) -> int:
    """List, add, or delete files on a physical device."""
    from .cli.main import _resolve_device_selector
    from gnwmanager.gnw import GnW

    row = _resolve_device_selector(device)
    if row.get("kind") != "hardware":
        raise ValueError("media operations on a running GWemu device are not supported yet")
    pending_files = pending_dirs = None
    if filesystem == "sd" and operation == "add":
        pending_files, pending_dirs = _files(source, path)
        if pending_dirs and not hasattr(GnW, "sd_mkdir"):
            raise RuntimeError(
                "the installed gnwmanager firmware has no SD mkdir command; "
                "nested device SD imports need the matching gnwmanager update"
            )
    backend = _backend(row)
    mark_ready = None
    try:
        backend.open()
        gnw = GnW(backend)
        mark_ready = _ready_flasher(backend, gnw, yes=yes)
        if filesystem == "frogfs":
            if operation in ("ls", "tree"):
                import tempfile
                from .deploy import inspect_device_extflash
                from .filesystem import operate

                info = inspect_device_extflash(backend, gnw)
                region = next(
                    (item for item in info["regions"] if item["content"].startswith("FrogFS")), None
                )
                if not region:
                    raise ValueError("device firmware does not declare a FrogFS region")
                data = _read_memory(
                    backend, 0x90000000 + int(region["offset"]), int(region["bytes"])
                )
                with tempfile.NamedTemporaryFile(
                    prefix="gwprov-device-frogfs-", suffix=".bin"
                ) as image:
                    image.write(data)
                    image.flush()
                    return operate(
                        operation=operation,
                        target="flash/ext",
                        image=image.name,
                        filesystem="frogfs",
                        offset=0,
                        size=int(region["bytes"]),
                        path=path,
                    )
            return _device_frogfs(
                backend,
                gnw,
                row,
                operation=operation,
                path=path,
                source=source,
                contiguous=contiguous,
                sentinel=sentinel,
            )
        if filesystem == "sd":
            target_path = _target_path(path)
            if operation in ("ls", "tree"):
                try:
                    listing = gnw.sd_list_dir(target_path)
                except Exception:
                    listing = ""
                if path not in ("", "/") and not listing.strip():
                    # A file path is valid for tree/ls too. Probe its parent and
                    # print the exact matching file when the directory lookup fails.
                    parent, name = target_path.rsplit("/", 1)
                    parent = parent or "/"
                    try:
                        siblings = gnw.sd_list_dir(parent)
                    except Exception as error:
                        raise ValueError(f"no SD file or directory at {target_path}") from error
                    entries = {line.strip().rstrip("\r") for line in siblings.splitlines()}
                    if name + "/" in entries:
                        from .filesystem import _print_listing

                        _print_listing([], path, tree=operation == "tree")
                        return 0
                    if name in entries:
                        from .filesystem import _print_listing

                        _print_listing(
                            [(target_path.lstrip("/"), "file")], path, tree=operation == "tree"
                        )
                        return 0
                    raise ValueError(f"no SD file or directory at {target_path}")
                if operation == "tree":
                    rows, pending = [], [target_path]
                    while pending:
                        current = pending.pop()
                        current_listing = gnw.sd_list_dir(current)
                        for entry in current_listing.splitlines():
                            entry = entry.strip().rstrip("\r")
                            if not entry:
                                continue
                            is_dir = entry.endswith("/")
                            child = current.rstrip("/") + "/" + entry.rstrip("/")
                            rows.append((child.lstrip("/"), "directory" if is_dir else "file"))
                            if is_dir:
                                pending.append(child)
                    from .filesystem import _print_listing

                    _print_listing(rows, path or "sdcard /", tree=True)
                else:
                    print(listing, end="" if listing.endswith("\n") or not listing else "\n")
                return 0
            if operation == "delete":
                gnw.sd_unlink(target_path)
                print(f"Deleted sdcard/{target_path.lstrip('/')}")
                return 0
            files, dirs = pending_files, pending_dirs
            for directory in dirs:
                try:
                    gnw.sd_list_dir(directory)
                except Exception:
                    mkdir = getattr(gnw, "sd_mkdir", None)
                    if mkdir is None:
                        raise RuntimeError(
                            "the installed gnwmanager firmware has no SD mkdir command; "
                            "create the target directory on a mounted card or update gnwmanager"
                        )
                    mkdir(directory)
            for local, remote in files:
                with local.open("rb") as stream:
                    gnw.sd_write_file(remote, stream.read())
            print(f"Added {len(files)} file(s) to sdcard/{path.strip('/') or ''}")
            return 0
        if filesystem != "littlefs":
            raise ValueError(f"unsupported device filesystem {filesystem!r}")
        from .deploy import inspect_device_extflash

        layout = inspect_device_extflash(backend, gnw)
        region = next(
            (item for item in layout["regions"] if item["content"].startswith("LittleFS")), None
        )
        if not region:
            raise ValueError("device firmware does not declare a LittleFS region")
        block_size = int(gnw.external_flash_block_size)
        size = int(region["bytes"])
        if size % block_size:
            raise ValueError(
                "device LittleFS size is not aligned to the external-flash erase block"
            )
        end_offset = int(gnw.external_flash_size) - int(region["offset"]) - size
        fs = gnw.filesystem(offset=end_offset, block_count=size // block_size)
        try:
            name = _target_path(path)
            if operation in ("ls", "tree"):
                rows = []
                try:
                    with fs.open(name, "rb"):
                        rows.append((name.lstrip("/"), "file"))
                    is_file = True
                except OSError:
                    is_file = False
                if not is_file:
                    for root, dirs, filenames in fs.walk(name):
                        rows.extend(
                            ((root.rstrip("/") + "/" + entry).lstrip("/"), "directory")
                            for entry in dirs
                        )
                        rows.extend(
                            ((root.rstrip("/") + "/" + entry).lstrip("/"), "file")
                            for entry in filenames
                        )
                from .filesystem import _print_listing

                _print_listing(rows, path or "LittleFS /", tree=operation == "tree")
                return 0
            if operation == "delete":
                fs.remove(name)
                print(f"Deleted littlefs/{name.lstrip('/')}")
                return 0
            files, dirs = _files(source, path)
            for directory in dirs:
                fs.makedirs(directory, exist_ok=True)
            for local, remote in files:
                fs.makedirs(str(PurePosixPath(remote).parent), exist_ok=True)
                with local.open("rb") as inp, fs.open(remote, "wb") as out:
                    shutil.copyfileobj(inp, out)
            print(f"Added {len(files)} file(s) to LittleFS")
            return 0
        finally:
            try:
                fs.unmount()
            except Exception:
                pass
    finally:
        if mark_ready is not None:
            try:
                mark_ready(backend)
            except Exception:
                pass
        backend.close()
