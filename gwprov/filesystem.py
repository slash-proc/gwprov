"""Filesystem operations on profile and standalone device images."""
from __future__ import annotations

import gzip
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
    path = PurePosixPath(value.replace("\\", "/").lstrip("/"))
    if not value or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"invalid filesystem path: {value!r}")
    return path.as_posix()


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
    lfs_size = int(layout.get("littlefsBytes", 0))
    ext_size = int(layout.get("extflashBytes", profile.extflash.stat().st_size))
    frog_size = int(layout.get("frogfsBytes", 0))
    lfs_offset = ext_size - lfs_size
    if lfs_size <= 0 or lfs_offset <= 0:
        raise ValueError("profile has no declared LittleFS region")
    return profile, profile.extflash, "frogfs", 0, {"lfs_offset": lfs_offset,
        "lfs_size": lfs_size, "frogfs_size": frog_size, "ext_size": ext_size,
        "block_size": int(metadata.get("firmware", {}).get("littlefsBlockSize", 4096))}


def _read_frogfs(image: Path, offset: int, destination: Path) -> None:
    from .inventory import frogfs
    listing = frogfs(image, offset)
    with image.open("rb") as stream:
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
            return "/".join(reversed(parts))

        path_to_position = {node_path(position): position for position in nodes}
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
            if compression == 0: data = stored
            elif compression == 1: data = zlib.decompress(stored)
            elif compression == 2:
                try: import heatshrink2
                except ImportError as exc: raise RuntimeError("extracting this FrogFS requires heatshrink2") from exc
                data = heatshrink2.decompress(stored, window_sz2=options & 0xF, lookahead_sz2=options >> 4)
            elif compression == 3: data = gzip.decompress(stored)
            else: raise ValueError(f"unsupported FrogFS compression type {compression}")
            path.write_bytes(data)


def _build_frogfs(source: Path, output: Path, work: Path) -> None:
    if not any(path.is_file() for path in source.rglob("*")):
        from .vendor.frogfs import format as frog_format
        from .vendor.frogfs.frogfs import djb2_hash, align
        root_offset = align(frog_format.head.size) + align(frog_format.hash.size)
        total = root_offset + align(frog_format.dir.size) + frog_format.foot.size
        data = (frog_format.head.pack(frog_format.FROGFS_MAGIC,
                                     frog_format.FROGFS_VER_MAJOR,
                                     frog_format.FROGFS_VER_MINOR, 1, total)
                + frog_format.hash.pack(djb2_hash(""), root_offset)
                + frog_format.dir.pack(0, 0, 0, 0))
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
    config.write_text(yaml.safe_dump({"collect": {str(source): ""}}), encoding="utf-8")
    subprocess.run([sys.executable, str(script), str(config), str(build), str(output)],
                   check=True, cwd=work)


def _mount_littlefs(image: Path, offset: int, size: int, block_size: int):
    from littlefs import LittleFS

    class Context:
        def __init__(self, stream): self.stream = stream
        def _seek(self, block, off):
            self.stream.seek(offset + size - (block + 1) * block_size + off)
        def read(self, cfg, block, off, length):
            self._seek(block, off); return bytearray(self.stream.read(length))
        def prog(self, cfg, block, off, data):
            self._seek(block, off); self.stream.write(data); return 0
        def erase(self, cfg, block):
            self._seek(block, 0); self.stream.write(b"\xff" * block_size); return 0
        def sync(self, cfg): self.stream.flush(); os.fsync(self.stream.fileno()); return 0

    stream = image.open("r+b")
    fs = LittleFS(context=Context(stream), mount=False, block_size=block_size,
                  block_count=size // block_size)
    try:
        fs.mount()
    except Exception:
        stream.close()
        raise
    return fs, stream


def operate(*, operation: str, target: str, profile: str | None = None,
            image: str | None = None, filesystem: str | None = None,
            offset: int = 0, size: int | None = None, block_size: int = 4096,
            path: str = "/", source: str | None = None,
            size_mib: int | None = None, force: bool = False) -> int:
    """List, add, or remove a file in a profile/image filesystem."""
    if profile:
        device, image_path, kind, offset, layout = _profile_region(profile, target)
        size = (layout["lfs_size"] if layout and target == "flash/ext" else None)
        block_size = layout["block_size"] if layout else block_size
        if target == "sd": kind = "fatfs"
        elif filesystem == "littlefs" and layout:
            kind, offset, size = "littlefs", layout["lfs_offset"], layout["lfs_size"]
        elif filesystem in (None, "frogfs") and layout:
            kind, offset = "frogfs", 0
        elif filesystem:
            kind = filesystem
    else:
        if not image or not filesystem:
            raise ValueError("specify --profile or both --image and --filesystem")
        device, image_path, kind, layout = None, Path(image).expanduser().resolve(), filesystem, None
    if operation == "create":
        if (profile or image_path.exists()) and not force:
            raise ValueError("create replaces existing filesystem data; repeat with --force to confirm")
        if kind in ("sd", "fatfs"):
            if profile:
                if target != "sd" or device.sd_mode != "bundled":
                    raise ValueError("creating SD filesystems requires a bundled SD profile image")
                if size_mib is None: raise ValueError("SD create requires --size-mib")
            elif size_mib is None:
                raise ValueError("SD create requires --size-mib")
            if size_mib <= 0: raise ValueError("SD size must be a positive number of MiB")
            from .common.sdcard import create_image
            image_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".gwprov-sd-", dir=image_path.parent) as temp:
                created = Path(temp) / "sdcard.img"
                create_image(str(created), size_mb=size_mib)
                os.replace(created, image_path)
            return 0
        if kind == "frogfs":
            if size is None and profile and layout:
                size = layout["lfs_offset"]
            if size is None: raise ValueError("FrogFS create requires --size")
            if size <= 0: raise ValueError("FrogFS size must be positive")
            if profile and layout:
                capacity = layout["lfs_offset"]
                if size > capacity: raise ValueError(f"FrogFS size exceeds profile capacity ({capacity} bytes)")
            elif not profile and image_path.exists() and offset + size > image_path.stat().st_size:
                raise ValueError("FrogFS region exceeds the existing image")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            base = image_path.parent
            with tempfile.TemporaryDirectory(prefix=".gwprov-fs-", dir=base) as temp:
                work = Path(temp); empty = work / "empty"; empty.mkdir()
                built = work / "frogfs.bin"
                _build_frogfs(empty, built, work)
                if built.stat().st_size > size: raise ValueError("FrogFS size is too small for an empty filesystem")
                region = built.read_bytes() + b"\xff" * (size - built.stat().st_size)
                if profile and layout:
                    ext = bytearray(image_path.read_bytes())
                    ext[:layout["lfs_offset"]] = b"\xff" * layout["lfs_offset"]
                    ext[:len(region)] = region
                    temp_image = work / "extflash.new"; temp_image.write_bytes(ext)
                    bank = bytearray(device.bank2.read_bytes())
                    candidates = [i for i in range(0, len(bank) - 35, 4)
                                  if bank[i:i+8] == b"GWLB\x02\x00\x24\x00"
                                  and zlib.crc32(bank[i:i+32]) == struct.unpack_from("<I", bank, i+32)[0]]
                    if len(candidates) != 1: raise ValueError("profile bank2 has no unique valid GWLB layout")
                    pos = candidates[0]; struct.pack_into("<I", bank, pos+12, built.stat().st_size)
                    struct.pack_into("<I", bank, pos+32, zlib.crc32(bank[pos:pos+32]))
                    new_bank = work / "bank2.new"; new_bank.write_bytes(bank)
                    metadata_path = device.root / "provision.json"
                    old_metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
                    if old_metadata is not None:
                        metadata = json.loads(old_metadata)
                        metadata.setdefault("layout", {})["frogfsBytes"] = built.stat().st_size
                        (work / "provision.new.json").write_text(json.dumps(metadata, indent=2) + "\n")
                    old_ext, old_bank = image_path.read_bytes(), device.bank2.read_bytes()
                    try:
                        os.replace(temp_image, image_path); os.replace(new_bank, device.bank2)
                        if old_metadata is not None:
                            os.replace(work / "provision.new.json", metadata_path)
                    except Exception:
                        image_path.write_bytes(old_ext); device.bank2.write_bytes(old_bank)
                        if old_metadata is not None: metadata_path.write_bytes(old_metadata)
                        raise
                else:
                    image_path.parent.mkdir(parents=True, exist_ok=True)
                    if not image_path.exists(): image_path.write_bytes(b"\xff" * (offset + size))
                    with image_path.open("r+b") as output:
                        output.seek(offset); output.write(region)
            return 0
        if kind == "littlefs":
            if profile and layout:
                if size is None: size = layout["lfs_size"]
                if size != layout["lfs_size"] or offset not in (0, layout["lfs_offset"]):
                    raise ValueError("profile LittleFS create must match its declared region size and offset")
                offset = layout["lfs_offset"]
            if size is None or size <= 0 or size % block_size:
                raise ValueError("LittleFS create needs a positive, block-aligned --size")
            image_path.parent.mkdir(parents=True, exist_ok=True)
            if not image_path.exists(): image_path.write_bytes(b"\xff" * (offset + size))
            if offset + size > image_path.stat().st_size: raise ValueError("LittleFS region exceeds image")
            with image_path.open("r+b") as stream:
                stream.seek(offset); stream.write(b"\xff" * size)
            from littlefs import LittleFS
            class FormatContext:
                def __init__(self, stream): self.stream = stream
                def _seek(self, block, off): self.stream.seek(offset + size - (block+1)*block_size + off)
                def read(self, cfg, block, off, length): self._seek(block, off); return bytearray(self.stream.read(length))
                def prog(self, cfg, block, off, data): self._seek(block, off); self.stream.write(data); return 0
                def erase(self, cfg, block): self._seek(block, 0); self.stream.write(b"\xff"*block_size); return 0
                def sync(self, cfg): self.stream.flush(); return 0
            with image_path.open("r+b") as stream:
                fs = LittleFS(context=FormatContext(stream), mount=False, block_size=block_size,
                              block_count=size//block_size)
                fs.format(); fs.mount(); fs.unmount()
            return 0
        raise ValueError("create supports FrogFS, LittleFS, or SD/FAT32")
    relative = _safe_name(path) if path not in ("", "/") else ""
    if kind == "frogfs":
        if operation == "ls":
            from .inventory import frogfs
            rows = frogfs(image_path, offset)["entries"]
            for row in rows:
                if not relative or row["path"] == relative or row["path"].startswith(relative + "/"):
                    print(("d " if row["type"] == "directory" else "f ") + row["path"])
            return 0
        if not device:
            raise ValueError("FrogFS add/remove requires a profile so its flash layout can be updated safely")
        source_path = Path(source).expanduser().resolve() if source else None
        with tempfile.TemporaryDirectory(prefix=".gwprov-fs-", dir=image_path.parent) as temp:
            work = Path(temp); tree = work / "tree"; tree.mkdir()
            _read_frogfs(image_path, offset, tree)
            dest = tree / relative
            if operation == "add":
                if source_path is None or not source_path.is_file():
                    raise ValueError("FrogFS add requires a local file source")
                dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(source_path, dest)
            elif operation == "delete":
                if not dest.is_file(): raise ValueError(f"FrogFS file does not exist: {relative}")
                dest.unlink()
            built = work / "frogfs.bin"
            _build_frogfs(tree, built, work)
            new_size = built.stat().st_size
            capacity = layout["lfs_offset"] if layout else (size or image_path.stat().st_size - offset)
            if capacity <= 0 or offset < 0 or offset + capacity > image_path.stat().st_size:
                raise ValueError("FrogFS region exceeds the image; specify a valid --offset and --size")
            if new_size > capacity: raise ValueError(f"rebuilt FrogFS needs {new_size} bytes; capacity is {capacity}")
            ext = bytearray(image_path.read_bytes())
            ext[offset:offset + capacity] = b"\xff" * capacity
            ext[offset:offset + new_size] = built.read_bytes()
            temp_image = work / "extflash.new"
            temp_image.write_bytes(ext)
            if not device:
                os.replace(temp_image, image_path)
                print(f"Rebuilt FrogFS ({new_size} bytes) in {image_path}")
                return 0
            # Patch the firmware's verified GWLB layout declaration with the new FrogFS size.
            bank = bytearray(device.bank2.read_bytes())
            candidates = [i for i in range(0, len(bank) - 35, 4)
                          if bank[i:i+8] == b"GWLB\x02\x00\x24\x00"
                          and zlib.crc32(bank[i:i+32]) == struct.unpack_from("<I", bank, i+32)[0]]
            if len(candidates) != 1: raise ValueError("profile bank2 has no unique valid GWLB layout")
            pos = candidates[0]; struct.pack_into("<I", bank, pos + 12, new_size)
            struct.pack_into("<I", bank, pos + 32, zlib.crc32(bank[pos:pos+32]))
            new_bank = work / "bank2.new"; new_bank.write_bytes(bank)
            old_ext = image_path.read_bytes()
            old_bank = device.bank2.read_bytes()
            metadata_path = device.root / "provision.json"
            old_metadata = metadata_path.read_bytes() if metadata_path.is_file() else None
            if old_metadata is not None:
                metadata = json.loads(old_metadata)
                metadata.setdefault("layout", {})["frogfsBytes"] = new_size
                (work / "provision.new.json").write_text(json.dumps(metadata, indent=2) + "\n")
            try:
                os.replace(temp_image, image_path)
                os.replace(new_bank, device.bank2)
                if old_metadata is not None:
                    os.replace(work / "provision.new.json", metadata_path)
            except Exception:
                image_path.write_bytes(old_ext)
                device.bank2.write_bytes(old_bank)
                if old_metadata is not None: metadata_path.write_bytes(old_metadata)
                raise
            print(f"Rebuilt FrogFS ({new_size} bytes) in profile {device.root}")
            return 0
    if kind == "littlefs":
        if size is None or size <= 0: raise ValueError("LittleFS operations require a positive --size")
        if size % block_size: raise ValueError("LittleFS region is not block aligned")
        fs, stream = _mount_littlefs(image_path, offset, size, block_size)
        try:
            name = "/" + relative if relative else "/"
            if operation == "ls":
                for root, dirs, files in fs.walk(name):
                    for entry in dirs: print((root.rstrip("/") + "/" + entry).lstrip("/"))
                    for entry in files: print((root.rstrip("/") + "/" + entry).lstrip("/"))
            elif operation == "add":
                if not source: raise ValueError("add requires --source")
                src = Path(source).expanduser()
                name = "/" + _safe_name(path)
                parent = str(PurePosixPath(name).parent)
                current = ""
                for component in parent.strip("/").split("/") if parent != "/" else ():
                    current += "/" + component
                    try: fs.stat(current)
                    except OSError: fs.mkdir(current)
                with src.open("rb") as inp, fs.open(name, "wb") as out: shutil.copyfileobj(inp, out)
            elif operation == "delete":
                fs.remove(name)
            fs.unmount()
        finally:
            try: fs.unmount()
            except Exception: pass
            stream.close()
        return 0
    if kind in ("fatfs", "sd"):
        if operation == "ls":
            from .common.sdcard import QemuSDCardManager
            manager = QemuSDCardManager(str(image_path), "@@1M" if target == "sd" or profile else "")
            print(manager.listing("/" + relative)); return 0
        if operation == "add":
            if not source: raise ValueError("add requires --source")
            from .common.sdcard import QemuSDCardManager
            manager = QemuSDCardManager(str(image_path), "@@1M" if target == "sd" or profile else "")
            parent = PurePosixPath(relative).parent
            current = PurePosixPath()
            for component in parent.parts:
                current /= component
                manager.mkdir(current.as_posix())
            manager.push_file(str(Path(source).expanduser()), relative); return 0
        if operation == "delete":
            from .common.sdcard import QemuSDCardManager
            manager = QemuSDCardManager(str(image_path), "@@1M" if target == "sd" or profile else "")
            manager.remove(relative); return 0
    raise ValueError(f"unsupported filesystem type {kind!r}")
