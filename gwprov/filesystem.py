"""Filesystem operations on profile and standalone device images."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path, PurePosixPath


def _safe_name(value: str) -> str:
    normalized = value.replace("\\", "/").lstrip("/")
    path = PurePosixPath(normalized)
    if not normalized or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"invalid filesystem path: {value!r}")
    return path.as_posix()


def _source_tree(source: str, destination: str) -> tuple[list[tuple[Path, str]], list[str]]:
    """Map a source file or directory onto a destination directory."""
    root = Path(source).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"media source does not exist: {root}")
    dest_root = (
        PurePosixPath("/")
        if destination in ("", "/")
        else PurePosixPath("/" + _safe_name(destination))
    )
    if root.is_file():
        return [(root, (dest_root / root.name).as_posix())], []
    if not root.is_dir():
        raise ValueError(f"media source is not a regular file or directory: {root}")
    tree_root = dest_root / root.name
    files: list[tuple[Path, str]] = []
    dirs: list[str] = [tree_root.as_posix()]
    for current, names, filenames in os.walk(root):
        current_path = Path(current)
        for name in names:
            if (current_path / name).is_symlink():
                raise ValueError(
                    f"media source contains a symlinked directory: {current_path / name}"
                )
        rel = current_path.relative_to(root)
        remote_dir = tree_root / PurePosixPath(rel.as_posix()) if rel.parts else tree_root
        dirs.append(remote_dir.as_posix())
        names.sort()
        for filename in sorted(filenames):
            local = current_path / filename
            if local.is_symlink() or not local.is_file():
                raise ValueError(f"media source contains a non-regular file: {local}")
            files.append((local, (remote_dir / filename).as_posix()))
    return files, sorted(set(dirs), key=lambda value: (value.count("/"), value))


def _relative_target(value: str) -> str:
    if value in ("", "/"):
        return ""
    return _safe_name(value)


def identify_filesystem_image(image: str | Path, *, hint: str | None = None) -> str:
    """Validate a standalone filesystem image and identify its on-disk type."""
    path = Path(image).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"filesystem image does not exist or is not a file: {path}")
    if hint not in {None, "frogfs", "littlefs", "sd"}:
        raise ValueError(f"unsupported blob filesystem hint: {hint}")
    if hint in (None, "frogfs"):
        try:
            from .inventory import frogfs

            report = frogfs(path, 0)
            size = int(report["bytes"])
            if size > path.stat().st_size or size < 16:
                raise ValueError("invalid FrogFS image size")
            with path.open("rb") as stream:
                remaining = size - 4
                checksum = 0
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("truncated FrogFS image")
                    checksum = zlib.crc32(chunk, checksum)
                    remaining -= len(chunk)
                expected = struct.unpack("<I", stream.read(4))[0]
            if checksum != expected:
                raise ValueError("invalid FrogFS checksum")
            return "frogfs"
        except Exception:
            if hint == "frogfs":
                raise ValueError(f"blob is not a valid FrogFS image: {path}")
    if hint in (None, "littlefs"):
        try:
            from .inventory import littlefs

            littlefs(path, 0, path.stat().st_size, 4096)
            return "littlefs"
        except Exception:
            if hint == "littlefs":
                raise ValueError(f"blob is not a valid LittleFS image: {path}")
    if hint in (None, "sd"):
        try:
            from pyfatfs.PyFatFS import PyFatFS

            with path.open("rb") as stream:
                header = stream.read(512)
            offsets = []
            if len(header) == 512 and header[510:512] == b"\x55\xaa":
                for index in range(4):
                    entry = header[446 + index * 16 : 462 + index * 16]
                    if entry[4] in (1, 4, 6, 11, 12, 14):
                        offsets.append(int.from_bytes(entry[8:12], "little") * 512)
            for offset in offsets or [0]:
                try:
                    with PyFatFS(str(path), offset=offset, read_only=True):
                        return "sd"
                except Exception:
                    continue
        except ImportError as error:
            if hint == "sd":
                raise RuntimeError("validating an SD blob requires pyfatfs") from error
        if hint == "sd":
            raise ValueError(f"blob is not a valid FAT SD image: {path}")
    raise ValueError(f"cannot identify a valid FrogFS, LittleFS, or FAT image: {path}")


def _print_listing(rows: list[tuple[str, str]], root: str, *, tree: bool) -> None:
    root_path = root.strip("/")
    if tree and root_path:
        rows = [row for row in rows if row[0].strip("/") != root_path]
    rows = sorted(set(rows), key=lambda item: (item[0].casefold(), item[1] != "directory"))
    if not tree:
        for path, kind in rows:
            print(("d " if kind == "directory" else "f ") + path)
        return
    from rich.console import Console
    from rich.tree import Tree

    label = root or "/"
    rendered = Tree(label)
    nodes = {"": rendered}
    for path, kind in rows:
        components = PurePosixPath(path).parts
        parent = ""
        for index, component in enumerate(components):
            key = "/".join(components[: index + 1])
            if key not in nodes:
                nodes[key] = nodes[parent].add(component)
            parent = key
    Console().print(rendered)


def _profile_region(profile_name: str, target: str):
    from .profiles import DeviceProfile

    profile = DeviceProfile.load(profile_name)
    if target == "sd":
        if profile.resolved_sd is None:
            raise ValueError("profile has no SD card image")
        return profile, profile.resolved_sd, "fatfs", 1024 * 1024, None
    if target != "flash/ext":
        raise ValueError("profile target must be flash/ext or sd")
    metadata_path = profile.root / "provision.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    layout = metadata.get("layout", {})
    firmware = metadata.get("firmware", {})
    if not isinstance(firmware, dict):
        firmware = {}
    lfs_size = int(layout.get("littlefsBytes", 0))
    ext_size = int(layout.get("extflashBytes", profile.extflash.stat().st_size))
    frog_offset = int(layout.get("frogfsOffset", -1))
    frog_size = int(layout.get("frogfsBytes", 0))
    lfs_offset = ext_size - lfs_size
    if lfs_size <= 0 or lfs_offset <= 0:
        raise ValueError("profile has no declared LittleFS region")
    if frog_offset < 0 or frog_size <= 0:
        raise ValueError("profile has no declared FrogFS offset and size")
    if frog_offset + frog_size > lfs_offset:
        raise ValueError("declared FrogFS region overlaps LittleFS or exceeds its boundary")
    return (
        profile,
        profile.extflash,
        "frogfs",
        frog_offset,
        {
            "frogfs_offset": frog_offset,
            "frogfs_size": frog_size,
            "frogfs_capacity": lfs_offset - frog_offset,
            "lfs_offset": lfs_offset,
            "lfs_size": lfs_size,
            "ext_size": ext_size,
            "block_size": int(firmware.get("littlefsBlockSize", 4096)),
        },
    )


def _read_frogfs(image: Path, offset: int, destination: Path) -> None:
    from .inventory import frogfs

    listing = frogfs(image, offset)
    with image.open("rb") as stream:
        stream.seek(offset)
        _, _, _, count, size = struct.unpack("<IBBHI", stream.read(12))
        table_offsets = [struct.unpack("<II", stream.read(8))[1] for _ in range(count)]
        nodes = {}
        for position in table_offsets:
            stream.seek(offset + position)
            parent, flags, name_len, options = struct.unpack("<IHBB", stream.read(8))
            is_file = flags & 0xFF00 == 0xFF00
            header_size = 20 if is_file and flags & 0xFF else 16 if is_file else 8 + 4 * flags
            stream.seek(offset + position + header_size)
            nodes[position] = (parent, stream.read(name_len).decode("utf-8"), flags, options)

        def node_path(position):
            parts, seen = [], set()
            while position:
                if position in seen or position not in nodes:
                    raise ValueError("invalid FrogFS parent chain")
                seen.add(position)
                parent, name, _, _ = nodes[position]
                parts.append(name)
                position = parent
            # FrogFS stores the root directory name as "/". The inventory
            # API exposes paths relative to that root, so normalize both to
            # the same relative form before matching file entries.
            return "/".join(reversed(parts)).strip("/")

        path_to_position = {
            node_path(position): position for position in nodes if node_path(position)
        }
        for row in listing["entries"]:
            path = destination / _safe_name(row["path"])
            if row["type"] == "directory":
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            position = path_to_position[row["path"]]
            parent, name, flags, options = nodes[position]
            stream.seek(offset + position + 8)
            start, stored_size = struct.unpack("<II", stream.read(8))
            stream.seek(offset + start)
            stored = stream.read(stored_size)
            compression = flags & 0xFF
            if compression == 0:
                data = stored
            elif compression == 1:
                data = zlib.decompress(stored)
            elif compression == 2:
                try:
                    import heatshrink2
                except ImportError as exc:
                    raise RuntimeError("extracting this FrogFS requires heatshrink2") from exc
                data = heatshrink2.decompress(
                    stored, window_sz2=options & 0xF, lookahead_sz2=options >> 4
                )
            elif compression == 3:
                data = gzip.decompress(stored)
            else:
                raise ValueError(f"unsupported FrogFS compression type {compression}")
            path.write_bytes(data)


def _mapped_artifacts(profile_root: Path) -> list[tuple[str, int]]:
    """Read mapped FrogFS payload declarations from generic profile metadata."""
    metadata_path = profile_root / "provision.json"
    if not metadata_path.is_file():
        return []
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    declarations = []
    projects = metadata.get("projects", {})
    if not isinstance(projects, dict):
        raise ValueError("profile provision.json projects field must be an object")
    seen = set()
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        for item in project.get("mapped", []):
            if not isinstance(item, dict) or "path" not in item or "relocBase" not in item:
                raise ValueError("mapped FrogFS declarations require path and relocBase")
            path = _safe_name(item["path"])
            parts = PurePosixPath(path).parts
            if not parts or parts[0] not in {"cores", "homebrews"} or ".." in parts:
                raise ValueError(f"invalid mapped FrogFS path: {path!r}")
            base = item["relocBase"]
            base = int(base, 0) if isinstance(base, str) else base
            if not isinstance(base, int) or not 0 <= base <= 0xFFFFFFFF:
                raise ValueError(f"invalid mapped FrogFS relocation base for {path}")
            if path in seen:
                raise ValueError(f"duplicate mapped FrogFS path: {path}")
            seen.add(path)
            declarations.append((path, base))
    for item in metadata.get("mediaSentinels", []):
        if not isinstance(item, dict) or "path" not in item or "relocBase" not in item:
            raise ValueError("mediaSentinels entries require path and relocBase")
        path = _safe_name(str(item["path"]))
        base = item["relocBase"]
        base = int(base, 0) if isinstance(base, str) else base
        if not isinstance(base, int) or not 0 <= base <= 0xFFFFFFFF:
            raise ValueError(f"invalid sentinel relocation base for {path}")
        if path in seen:
            raise ValueError(f"duplicate mapped FrogFS path: {path}")
        seen.add(path)
        declarations.append((path, base))
    return declarations


def _sync_flash_map_component(
    profile_root: Path, *, offset: int, data: bytes, label: str, filename: str
) -> None:
    """Keep hardware component blobs and their YAML hashes aligned with extflash."""
    map_path = profile_root / "flash-map.yaml"
    if not map_path.is_file():
        return
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("updating profile flash-map.yaml requires PyYAML") from exc
    flash_map = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    if not isinstance(flash_map, dict):
        raise ValueError("profile flash-map.yaml must contain a mapping")
    if "bank0" in flash_map and "extflash" in flash_map:
        raise ValueError("flash map cannot define both bank0 and its extflash alias")
    entries = flash_map.setdefault("bank0", flash_map.pop("extflash", []))
    if not isinstance(entries, list):
        raise ValueError("flash-map bank0 must be a list")
    matches = [
        entry
        for entry in entries
        if isinstance(entry, dict) and int(entry.get("offset", -1)) == offset
    ]
    if len(matches) > 1:
        raise ValueError(f"flash-map has multiple extflash components at offset 0x{offset:X}")
    if matches:
        entry = matches[0]
        relative = Path(str(entry.get("file", "")))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError(f"invalid flash-map component path: {relative}")
    else:
        relative = Path("blobs") / filename
        entry = {"name": label, "file": relative.as_posix(), "offset": offset}
        entries.append(entry)
    blob_path = (profile_root / relative).resolve()
    if profile_root.resolve() not in blob_path.parents:
        raise ValueError(f"flash-map component path escapes profile: {relative}")
    blob_path.parent.mkdir(parents=True, exist_ok=True)
    blob_path.write_bytes(data)
    entry.update(
        {
            "name": entry.get("name") or label,
            "file": relative.as_posix(),
            "offset": offset,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    )
    map_path.write_text(yaml.safe_dump(flash_map, sort_keys=False), encoding="utf-8")


def _build_frogfs(
    source: Path,
    output: Path,
    work: Path,
    *,
    mapped_artifacts: list[tuple[str, int]] | None = None,
    contiguous_paths: list[str] | None = None,
    extflash_base: int = 0x90000000,
    extflash_offset: int = 0,
) -> None:
    if not any(path.is_file() for path in source.rglob("*")):
        from .vendor.frogfs import format as frog_format
        from .vendor.frogfs.frogfs import djb2_hash, align

        root_offset = align(frog_format.head.size) + align(frog_format.hash.size)
        total = root_offset + align(frog_format.dir.size) + frog_format.foot.size
        data = (
            frog_format.head.pack(
                frog_format.FROGFS_MAGIC,
                frog_format.FROGFS_VER_MAJOR,
                frog_format.FROGFS_VER_MINOR,
                1,
                total,
            )
            + frog_format.hash.pack(djb2_hash(""), root_offset)
            + frog_format.dir.pack(0, 0, 0, 0)
        )
        data += frog_format.foot.pack(zlib.crc32(data) & 0xFFFFFFFF)
        output.write_bytes(data)
        return
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("FrogFS rebuilding requires PyYAML") from exc
    script = Path(__file__).parent / "vendor/frogfs/mkfrogfs.py"
    config = work / "frogfs.yml"
    build = work / "build"
    # Collect children so a temporary staging directory name never becomes
    # an unexpected top-level filesystem directory.
    config_data = {"collect": {str(source / "*"): ""}}
    mapped_artifacts = mapped_artifacts or []
    no_compress = {path for path, _base in (mapped_artifacts or [])}
    no_compress.update(path.lstrip("/") for path in (contiguous_paths or []))
    if no_compress:
        config_data["filter"] = {path: ["no compress"] for path in sorted(no_compress)}
    config.write_text(yaml.safe_dump(config_data), encoding="utf-8")
    # mkfrogfs creates its cache directory as a child of build_dir, so the
    # parent must exist before its first Stage 1 directory entry is visited.
    build.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [sys.executable, str(script), str(config), str(build), str(output)], check=True, cwd=work
    )
    if mapped_artifacts:
        scripts = Path(__file__).parent / "vendor/retrogo_sd/scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        from frogfs_pico8_ro import patch_frogfs_mapped_inplace

        for logical_path, reloc_base in mapped_artifacts:
            if not patch_frogfs_mapped_inplace(
                output,
                logical_path=logical_path,
                reloc_base=reloc_base,
                extflash_base=extflash_base,
                extflash_offset=extflash_offset,
            ):
                raise ValueError(f"could not relocate mapped FrogFS payload: {logical_path}")


def _mount_littlefs(image: Path, offset: int, size: int, block_size: int):
    from littlefs import LittleFS

    class Context:
        def __init__(self, stream):
            self.stream = stream

        def _seek(self, block, off):
            self.stream.seek(offset + size - (block + 1) * block_size + off)

        def read(self, cfg, block, off, length):
            self._seek(block, off)
            return bytearray(self.stream.read(length))

        def prog(self, cfg, block, off, data):
            self._seek(block, off)
            self.stream.write(data)
            return 0

        def erase(self, cfg, block):
            self._seek(block, 0)
            self.stream.write(b"\xff" * block_size)
            return 0

        def sync(self, cfg):
            self.stream.flush()
            os.fsync(self.stream.fileno())
            return 0

    stream = image.open("r+b")
    fs = LittleFS(
        context=Context(stream), mount=False, block_size=block_size, block_count=size // block_size
    )
    try:
        fs.mount()
    except Exception:
        stream.close()
        raise
    return fs, stream


def operate(
    *,
    operation: str,
    target: str,
    profile: str | None = None,
    image: str | None = None,
    filesystem: str | None = None,
    offset: int = 0,
    size: int | None = None,
    block_size: int = 4096,
    path: str = "/",
    source: str | None = None,
    mapped_sources: list[str] | None = None,
    size_mib: int | None = None,
    force: bool = False,
    contiguous: bool = False,
    sentinel: int | None = None,
) -> int:
    """List, add, or remove a file in a profile/image filesystem."""
    if profile:
        device, image_path, kind, offset, layout = _profile_region(profile, target)
        size = layout["lfs_size"] if layout and target == "flash/ext" else None
        block_size = layout["block_size"] if layout else block_size
        if target == "sd":
            kind = "fatfs"
        elif filesystem == "littlefs" and layout:
            kind, offset, size = "littlefs", layout["lfs_offset"], layout["lfs_size"]
        elif filesystem in (None, "frogfs") and layout:
            kind, offset = "frogfs", layout["frogfs_offset"]
        elif filesystem:
            kind = filesystem
    else:
        if not image or not filesystem:
            raise ValueError("specify --profile or both --image and --filesystem")
        device, image_path, kind, layout = (
            None,
            Path(image).expanduser().resolve(),
            filesystem,
            None,
        )
    if operation == "create":
        if (profile or image_path.exists()) and not force:
            raise ValueError(
                "create replaces existing filesystem data; repeat with --force to confirm"
            )
        if kind in ("sd", "fatfs"):
            if profile:
                if target != "sd" or device.sd_mode != "bundled":
                    raise ValueError("creating SD filesystems requires a bundled SD profile image")
                if size_mib is None:
                    raise ValueError("SD create requires --size-mib")
            elif size_mib is None:
                raise ValueError("SD create requires --size-mib")
            if size_mib <= 0:
                raise ValueError("SD size must be a positive number of MiB")
            from .common.sdcard import create_image

            image_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".gwprov-sd-", dir=image_path.parent) as temp:
                created = Path(temp) / "sdcard.img"
                create_image(str(created), size_mb=size_mib)
                os.replace(created, image_path)
            return 0
        if kind == "frogfs":
            if profile and target == "flash/ext" and _mapped_artifacts(device.root):
                raise ValueError(
                    "cannot format a FrogFS profile while it declares mapped XiP artifacts"
                )
            if size is None and profile and layout:
                size = layout["frogfs_capacity"]
            if size is None:
                raise ValueError("FrogFS create requires --size")
            if size <= 0:
                raise ValueError("FrogFS size must be positive")
            if profile and layout:
                capacity = layout["frogfs_capacity"]
                if size > capacity:
                    raise ValueError(f"FrogFS size exceeds profile capacity ({capacity} bytes)")
            elif not profile and image_path.exists() and offset + size > image_path.stat().st_size:
                raise ValueError("FrogFS region exceeds the existing image")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            base = image_path.parent
            with tempfile.TemporaryDirectory(prefix=".gwprov-fs-", dir=base) as temp:
                work = Path(temp)
                empty = work / "empty"
                empty.mkdir()
                built = work / "frogfs.bin"
                _build_frogfs(empty, built, work)
                if built.stat().st_size > size:
                    raise ValueError("FrogFS size is too small for an empty filesystem")
                region = built.read_bytes() + b"\xff" * (size - built.stat().st_size)
                if profile and layout:
                    ext = bytearray(image_path.read_bytes())
                    start = layout["frogfs_offset"]
                    end = layout["lfs_offset"]
                    ext[start:end] = b"\xff" * (end - start)
                    ext[start : start + len(region)] = region
                    temp_image = work / "extflash.new"
                    temp_image.write_bytes(ext)
                    bank = bytearray(device.bank2.read_bytes())
                    candidates = [
                        i
                        for i in range(0, len(bank) - 35, 4)
                        if bank[i : i + 8] == b"GWLB\x02\x00\x24\x00"
                        and zlib.crc32(bank[i : i + 32])
                        == struct.unpack_from("<I", bank, i + 32)[0]
                    ]
                    if len(candidates) != 1:
                        raise ValueError("profile bank2 has no unique valid GWLB layout")
                    pos = candidates[0]
                    struct.pack_into("<I", bank, pos + 12, built.stat().st_size)
                    struct.pack_into("<I", bank, pos + 32, zlib.crc32(bank[pos : pos + 32]))
                    new_bank = work / "bank2.new"
                    new_bank.write_bytes(bank)
                    metadata_path = device.root / "provision.json"
                    old_metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
                    if old_metadata is not None:
                        metadata = json.loads(old_metadata)
                        metadata.setdefault("layout", {})["frogfsBytes"] = built.stat().st_size
                        (work / "provision.new.json").write_text(
                            json.dumps(metadata, indent=2) + "\n"
                        )
                    old_ext, old_bank = image_path.read_bytes(), device.bank2.read_bytes()
                    try:
                        os.replace(temp_image, image_path)
                        os.replace(new_bank, device.bank2)
                        if old_metadata is not None:
                            os.replace(work / "provision.new.json", metadata_path)
                    except Exception:
                        image_path.write_bytes(old_ext)
                        device.bank2.write_bytes(old_bank)
                        if old_metadata is not None:
                            metadata_path.write_bytes(old_metadata)
                        raise
                else:
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    if not image_path.exists():
                        image_path.write_bytes(b"\xff" * (offset + size))
                    with image_path.open("r+b") as output:
                        output.seek(offset)
                        output.write(region)
            return 0
        if kind == "littlefs":
            if profile and layout:
                if size is None:
                    size = layout["lfs_size"]
                if size != layout["lfs_size"] or offset not in (0, layout["lfs_offset"]):
                    raise ValueError(
                        "profile LittleFS create must match its declared region size and offset"
                    )
                offset = layout["lfs_offset"]
            if size is None or size <= 0 or size % block_size:
                raise ValueError("LittleFS create needs a positive, block-aligned --size")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            if not image_path.exists():
                image_path.write_bytes(b"\xff" * (offset + size))
            if offset + size > image_path.stat().st_size:
                raise ValueError("LittleFS region exceeds image")
            with image_path.open("r+b") as stream:
                stream.seek(offset)
                stream.write(b"\xff" * size)
            from littlefs import LittleFS

            class FormatContext:
                def __init__(self, stream):
                    self.stream = stream

                def _seek(self, block, off):
                    self.stream.seek(offset + size - (block + 1) * block_size + off)

                def read(self, cfg, block, off, length):
                    self._seek(block, off)
                    return bytearray(self.stream.read(length))

                def prog(self, cfg, block, off, data):
                    self._seek(block, off)
                    self.stream.write(data)
                    return 0

                def erase(self, cfg, block):
                    self._seek(block, 0)
                    self.stream.write(b"\xff" * block_size)
                    return 0

                def sync(self, cfg):
                    self.stream.flush()
                    return 0

            with image_path.open("r+b") as stream:
                fs = LittleFS(
                    context=FormatContext(stream),
                    mount=False,
                    block_size=block_size,
                    block_count=size // block_size,
                )
                fs.format()
                fs.mount()
                fs.unmount()
            return 0
        raise ValueError("create supports FrogFS, LittleFS, or SD/FAT32")
    relative = _relative_target(path)
    if kind == "frogfs":
        if operation in ("ls", "tree"):
            from .inventory import frogfs

            rows = frogfs(image_path, offset)["entries"]
            selected = [
                row
                for row in rows
                if not relative or row["path"] == relative or row["path"].startswith(relative + "/")
            ]
            if relative and not any(row["path"] == relative for row in selected):
                raise ValueError(f"no FrogFS file or directory at {relative}")
            _print_listing(
                [(row["path"], row["type"]) for row in selected],
                relative or "FrogFS /",
                tree=operation == "tree",
            )
            return 0
        source_path = Path(source).expanduser().resolve() if source else None
        with tempfile.TemporaryDirectory(prefix=".gwprov-fs-", dir=image_path.parent) as temp:
            work = Path(temp)
            tree = work / "tree"
            tree.mkdir()
            _read_frogfs(image_path, offset, tree)
            metadata_path = device.root / "provision.json" if device else None
            metadata = (
                json.loads(metadata_path.read_text())
                if metadata_path and metadata_path.is_file()
                else {}
            )
            manual_sentinels = metadata.get("mediaSentinels", [])
            if not isinstance(manual_sentinels, list):
                raise ValueError("profile provision.json mediaSentinels must be a list")
            mapped_artifacts = (
                _mapped_artifacts(device.root) if device and target == "flash/ext" else []
            )
            if operation == "add":
                if source_path is None:
                    raise ValueError("media add requires a source file or directory")
                if sentinel is not None and not source_path.is_file():
                    raise ValueError("--sentinel can only be used with a single file source")
                file_items, dir_items = _source_tree(str(source_path), path)
                for directory in dir_items:
                    (tree / _safe_name(directory)).mkdir(parents=True, exist_ok=True)
                for local, remote in file_items:
                    dest = tree / _safe_name(remote)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(local, dest)
                changed_paths = {remote for _local, remote in file_items}
            elif operation == "delete":
                dest = tree / relative
                if not dest.is_file():
                    raise ValueError(f"FrogFS file does not exist: {relative}")
                manual_paths = {_safe_name(str(item.get("path", ""))) for item in manual_sentinels}
                if (
                    relative
                    in {mapped_path for mapped_path, _base in mapped_artifacts} - manual_paths
                ):
                    raise ValueError(
                        "cannot delete a mapped FrogFS artifact while its profile declares it"
                    )
                dest.unlink()
                changed_paths = {relative}
            else:
                changed_paths = set()
            retained_sentinels = []
            manual_source_files = {}
            for item in manual_sentinels:
                mapped_path = _safe_name(str(item.get("path", "")))
                if mapped_path in changed_paths:
                    continue
                retained_sentinels.append(item)
                raw_source = Path(str(item.get("source", "")))
                if raw_source.is_absolute() or ".." in raw_source.parts or not raw_source.parts:
                    raise ValueError(
                        f"invalid sentinel source path in profile metadata: {raw_source}"
                    )
                if not device:
                    raise ValueError(
                        "standalone FrogFS images cannot contain profile-managed sentinels"
                    )
                source_file = device.root / raw_source
                if not source_file.is_file():
                    raise ValueError(f"sentinel source file is missing: {source_file}")
                manual_source_files[mapped_path] = source_file
                target_file = tree / mapped_path
                target_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_file, target_file)
            manual_paths = {_safe_name(str(item.get("path", ""))) for item in manual_sentinels}
            mapped_artifacts = [
                (mapped_path, base)
                for mapped_path, base in mapped_artifacts
                if mapped_path not in manual_paths or mapped_path not in changed_paths
            ]
            supplied_mapped = {}
            supplied_mapped.update(manual_source_files)
            for spec in mapped_sources or []:
                if "=" not in spec:
                    raise ValueError("--mapped-source format is FROGFS_PATH=LOCAL_FILE")
                mapped_path, local_path = spec.split("=", 1)
                mapped_path = _safe_name(mapped_path)
                if mapped_path in supplied_mapped:
                    raise ValueError(f"mapped FrogFS source supplied twice: {mapped_path}")
                file_path = Path(local_path).expanduser().resolve()
                if not file_path.is_file():
                    raise ValueError(f"mapped FrogFS source does not exist: {file_path}")
                supplied_mapped[mapped_path] = file_path
            if operation == "add" and sentinel is not None:
                if len(file_items) != 1:
                    raise ValueError("--sentinel requires exactly one file")
                logical_path = file_items[0][1]
                sidecar_name = (
                    hashlib.sha256(logical_path.encode("utf-8")).hexdigest()[:20] + ".bin"
                )
                sidecar_relative = Path("media-sentinels") / sidecar_name
                retained_sentinels.append(
                    {
                        "path": logical_path,
                        "relocBase": sentinel,
                        "source": sidecar_relative.as_posix(),
                    }
                )
                mapped_artifacts.append((logical_path, sentinel))
                supplied_mapped[logical_path] = source_path
            mapped_paths = {mapped_path for mapped_path, _base in mapped_artifacts}
            if operation == "add":
                for _local, remote in file_items:
                    if remote in mapped_paths:
                        raise ValueError(
                            "adding a mapped XiP artifact requires --mapped-source for its raw payload; "
                            "mapped files cannot be installed through the generic media command"
                        )
            unknown = set(supplied_mapped) - mapped_paths
            if unknown:
                raise ValueError(
                    "--mapped-source path is not declared by this profile: "
                    + ", ".join(sorted(unknown))
                )
            missing = mapped_paths - set(supplied_mapped)
            if missing:
                raise ValueError(
                    "FrogFS rebuild would move mapped payloads; supply each raw mapped sidecar "
                    "with --mapped-source PATH=LOCAL_FILE: " + ", ".join(sorted(missing))
                )
            for mapped_path, local_path in supplied_mapped.items():
                target_path = tree / _safe_name(mapped_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(local_path, target_path)
            built = work / "frogfs.bin"
            contiguous_paths = (
                [remote for _local, remote in file_items]
                if operation == "add" and contiguous
                else []
            )
            _build_frogfs(
                tree,
                built,
                work,
                mapped_artifacts=mapped_artifacts,
                contiguous_paths=contiguous_paths,
                extflash_offset=offset,
            )
            sidecar_staged = None
            if operation == "add" and sentinel is not None:
                sidecar_staged = work / sidecar_relative
                sidecar_staged.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, sidecar_staged)
            new_size = built.stat().st_size
            capacity = (
                layout["frogfs_capacity"]
                if layout
                else (size or image_path.stat().st_size - offset)
            )
            if capacity <= 0 or offset < 0 or offset + capacity > image_path.stat().st_size:
                raise ValueError(
                    "FrogFS region exceeds the image; specify a valid --offset and --size"
                )
            if new_size > capacity:
                raise ValueError(f"rebuilt FrogFS needs {new_size} bytes; capacity is {capacity}")
            ext = bytearray(image_path.read_bytes())
            ext[offset : offset + capacity] = b"\xff" * capacity
            ext[offset : offset + new_size] = built.read_bytes()
            temp_image = work / "extflash.new"
            temp_image.write_bytes(ext)
            if not device:
                os.replace(temp_image, image_path)
                print(f"Rebuilt FrogFS ({new_size} bytes) in {image_path}")
                return 0
            # Patch the firmware's verified GWLB layout declaration with the new FrogFS size.
            bank = bytearray(device.bank2.read_bytes())
            candidates = [
                i
                for i in range(0, len(bank) - 35, 4)
                if bank[i : i + 8] == b"GWLB\x02\x00\x24\x00"
                and zlib.crc32(bank[i : i + 32]) == struct.unpack_from("<I", bank, i + 32)[0]
            ]
            if len(candidates) != 1:
                raise ValueError("profile bank2 has no unique valid GWLB layout")
            pos = candidates[0]
            struct.pack_into("<I", bank, pos + 12, new_size)
            struct.pack_into("<I", bank, pos + 32, zlib.crc32(bank[pos : pos + 32]))
            new_bank = work / "bank2.new"
            new_bank.write_bytes(bank)
            old_ext = image_path.read_bytes()
            old_bank = device.bank2.read_bytes()
            old_metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
            metadata.setdefault("layout", {})["frogfsBytes"] = new_size
            metadata["mediaSentinels"] = retained_sentinels
            write_metadata = (
                old_metadata is not None or bool(retained_sentinels) or sentinel is not None
            )
            if write_metadata:
                (work / "provision.new.json").write_text(json.dumps(metadata, indent=2) + "\n")
            sidecar_target = device.root / sidecar_relative if sidecar_staged else None
            old_sidecar = (
                sidecar_target.read_bytes() if sidecar_target and sidecar_target.is_file() else None
            )
            try:
                os.replace(temp_image, image_path)
                os.replace(new_bank, device.bank2)
                if sidecar_target and sidecar_staged:
                    sidecar_target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(sidecar_staged, sidecar_target)
                if write_metadata:
                    os.replace(work / "provision.new.json", metadata_path)
            except Exception:
                image_path.write_bytes(old_ext)
                device.bank2.write_bytes(old_bank)
                if old_metadata is not None:
                    metadata_path.write_bytes(old_metadata)
                elif write_metadata and metadata_path.exists():
                    metadata_path.unlink()
                if sidecar_target:
                    if old_sidecar is not None:
                        sidecar_target.write_bytes(old_sidecar)
                    elif sidecar_target.exists():
                        sidecar_target.unlink()
                raise
            _sync_flash_map_component(
                device.root,
                offset=offset,
                data=built.read_bytes(),
                label="Retro-Go FrogFS",
                filename="frogfs.bin",
            )
            from .profiles import profile_reference

            print(f"Rebuilt FrogFS ({new_size} bytes) in profile {profile_reference(device.root)}")
            return 0
    if kind == "littlefs":
        if size is None or size <= 0:
            raise ValueError("LittleFS operations require a positive --size")
        if size % block_size:
            raise ValueError("LittleFS region is not block aligned")
        fs, stream = _mount_littlefs(image_path, offset, size, block_size)
        try:
            name = "/" + relative if relative else "/"
            if operation in ("ls", "tree"):
                rows = []
                if relative:
                    try:
                        with fs.open(name, "rb"):
                            rows.append((relative, "file"))
                        is_file = True
                    except (OSError, ValueError):
                        is_file = False
                else:
                    is_file = False
                if not is_file:
                    for root, dirs, files in fs.walk(name):
                        rows.extend(
                            ((root.rstrip("/") + "/" + entry).lstrip("/"), "directory")
                            for entry in dirs
                        )
                        rows.extend(
                            ((root.rstrip("/") + "/" + entry).lstrip("/"), "file")
                            for entry in files
                        )
                _print_listing(rows, relative or "LittleFS /", tree=operation == "tree")
            elif operation == "add":
                if not source:
                    raise ValueError("media add requires a source file or directory")
                file_items, dir_items = _source_tree(source, path)
                for directory in dir_items:
                    fs.makedirs("/" + _safe_name(directory), exist_ok=True)
                for local, remote in file_items:
                    name = "/" + _safe_name(remote)
                    parent = str(PurePosixPath(name).parent)
                    fs.makedirs(parent, exist_ok=True)
                    with local.open("rb") as inp, fs.open(name, "wb") as out:
                        shutil.copyfileobj(inp, out)
            elif operation == "delete":
                fs.remove(name)
        finally:
            try:
                fs.unmount()
            except Exception:
                pass
            stream.close()
        if profile and target == "flash/ext" and layout and kind == "littlefs":
            region = image_path.read_bytes()[offset : offset + size]
            _sync_flash_map_component(
                device.root,
                offset=offset,
                data=region,
                label="Retro-Go LittleFS",
                filename="littlefs.bin",
            )
        return 0
    if kind in ("fatfs", "sd"):
        if operation in ("ls", "tree"):
            from .common.sdcard import QemuSDCardManager

            manager = QemuSDCardManager(
                str(image_path), "@@1M" if target == "sd" or profile else ""
            )
            listing = manager.listing("/" + relative if relative else "/")
            if relative and not listing.strip():
                parent, name = relative.rsplit("/", 1) if "/" in relative else ("", relative)
                siblings = manager.listing("/" + parent if parent else "/")
                if name.casefold() in siblings.casefold():
                    print(relative)
                    return 0
                raise ValueError(f"no SD-card file or directory at {relative}")
            print(listing, end="" if listing.endswith("\n") or not listing else "\n")
            return 0
        if operation == "add":
            if not source:
                raise ValueError("media add requires a source file or directory")
            from .common.sdcard import QemuSDCardManager

            manager = QemuSDCardManager(
                str(image_path), "@@1M" if target == "sd" or profile else ""
            )
            file_items, dir_items = _source_tree(source, path)
            for directory in dir_items:
                manager.mkdir(_safe_name(directory))
            for local, remote in file_items:
                parent = PurePosixPath(remote).parent
                current = PurePosixPath()
                for component in parent.parts:
                    current /= component
                    manager.mkdir(current.as_posix())
                manager.push_file(str(local), _safe_name(remote))
            return 0
        if operation == "delete":
            from .common.sdcard import QemuSDCardManager

            manager = QemuSDCardManager(
                str(image_path), "@@1M" if target == "sd" or profile else ""
            )
            manager.remove(relative)
            return 0
    raise ValueError(f"unsupported filesystem type {kind!r}")
