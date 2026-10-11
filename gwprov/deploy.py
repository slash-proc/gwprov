"""Plan and apply profile deployment to hardware through gnwmanager."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import shutil
import struct
from typing import Callable, Iterable
import zlib

def _hash_range(path: Path, offset: int, size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        stream.seek(offset)
        remaining = size
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError(f"truncated deployment image: {path}")
            digest.update(chunk)
            remaining -= len(chunk)
    return digest.hexdigest()


def deployment_plan(profile_path: str | Path, regions: Iterable[str] | None = None) -> dict:
    """Describe exactly which profile bytes go to which hardware region."""
    from .profiles import DeviceProfile

    profile = DeviceProfile.load(profile_path)
    metadata_path = profile.root / "provision.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    layout = metadata.get("layout", {})
    candidates = []

    # Profiles keep component blobs separately from their padded GWemu images.
    # The YAML map is the hardware write contract.
    flash_map_path = profile.root / "flash-map.yaml"
    flash_map = None
    if flash_map_path.is_file():
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("reading profile flash-map.yaml requires PyYAML") from exc
        flash_map = yaml.safe_load(flash_map_path.read_text(encoding="utf-8"))
        if not isinstance(flash_map, dict):
            raise ValueError("profile flash-map.yaml must contain a mapping")
        if "bank0" in flash_map and "extflash" in flash_map:
            raise ValueError("flash map cannot define both bank0 and its extflash alias")
        if "extflash" in flash_map:
            flash_map["bank0"] = flash_map.pop("extflash")
        extflash_limit = 256 * 1024 * 1024
        if profile.extflash.is_file():
            extflash_limit = min(extflash_limit, profile.extflash.stat().st_size)
        for bank_key in ("bank1", "bank2", "bank0"):
            entries = flash_map.get(bank_key, [])
            if not isinstance(entries, list):
                raise ValueError(f"flash-map {bank_key} must be a list")
            for part in entries:
                if not isinstance(part, dict) or not all(
                        key in part for key in ("name", "file", "offset", "size", "sha256")):
                    raise ValueError(f"each flash-map {bank_key} entry needs name, file, offset, size, and sha256")
                region = "extflash" if bank_key == "bank0" else bank_key
                path = (profile.root / str(part["file"])).resolve()
                if profile.root not in path.parents or not path.is_file():
                    raise ValueError(f"profile flash-map blob is missing or escapes profile: {part['file']}")
                offset, size = int(part["offset"]), path.stat().st_size
                expected_size = int(part.get("size", size))
                if bank_key == "bank0":
                    if (offset == int(layout.get("frogfsOffset", -1))
                            and expected_size == int(layout.get("frogfsBytes", -1))):
                        region = "frogfs"
                    elif (offset == int(layout.get("littlefsOffset", -1))
                            and expected_size == int(layout.get("littlefsBytes", -1))):
                        region = "littlefs"
                digest = _hash_range(path, 0, size)
                expected_hash = str(part["sha256"]).casefold()
                if expected_size != size:
                    raise ValueError(f"flash-map size mismatch for {part['file']}: declared {expected_size}, found {size}")
                if len(expected_hash) != 64 or expected_hash != digest:
                    raise ValueError(f"flash-map SHA-256 mismatch for {part['file']}")
                bank = 0 if bank_key == "bank0" else int(bank_key[-1])
                if offset < 0 or size <= 0:
                    raise ValueError(f"flash-map {bank_key} entries need a non-negative offset and non-empty blob")
                if bank and offset + size > 256 * 1024:
                    raise ValueError(f"flash-map {bank_key} entry exceeds the 256 KiB internal flash bank")
                if not bank and offset + size > extflash_limit:
                    raise ValueError(
                        f"flash-map {bank_key} entry {part['name']!r} ends at 0x{offset + size:X}, "
                        f"past the profile extflash limit of 0x{extflash_limit:X}")
                candidates.append({"region": region, "kind": "internal-flash" if bank else "external-flash",
                                   "bank": bank, "offset": offset, "bytes": size,
                                   "path": str(path), "sha256": digest,
                                   "sourceOffset": 0, "content": str(part["name"])})
        for bank_key in ("bank1", "bank2", "bank0"):
            bank_number = 0 if bank_key == "bank0" else int(bank_key[-1])
            ranges = sorted((row["offset"], row["offset"] + row["bytes"])
                            for row in candidates if row["bank"] == bank_number)
            for previous, current in zip(ranges, ranges[1:]):
                if current[0] < previous[1]:
                    raise ValueError(f"flash-map has overlapping writes in {bank_key}")

    for name, path, bank in (() if flash_map is not None else (("bank1", profile.bank1, 1), ("bank2", profile.bank2, 2))):
        if path.is_file():
            size = path.stat().st_size
            if size > 256 * 1024:
                raise ValueError(f"{name} image is larger than an internal flash bank")
            candidates.append({"region": name, "kind": "internal-flash", "bank": bank,
                               "offset": 0, "bytes": size, "path": str(path),
                               "sha256": _hash_range(path, 0, size)})

    if flash_map is None and (metadata.get("firmware") == "stock" or metadata.get("model") in {"mario", "zelda"}):
        if profile.extflash.is_file():
            size = profile.extflash.stat().st_size
            candidates.append({"region": "extflash", "kind": "external-flash", "bank": 0,
                               "offset": 0, "bytes": size, "path": str(profile.extflash),
                               "sha256": _hash_range(profile.extflash, 0, size)})
    elif flash_map is None:
        if layout.get("frogfsBytes", 0):
            size = int(layout["frogfsBytes"])
            candidates.append({"region": "frogfs", "kind": "external-flash", "bank": 0,
                               "offset": 0, "bytes": size, "path": str(profile.extflash),
                               "sha256": _hash_range(profile.extflash, 0, size)})
        if layout.get("littlefsBytes", 0):
            size, offset = int(layout["littlefsBytes"]), int(layout["littlefsOffset"])
            candidates.append({"region": "littlefs", "kind": "external-flash", "bank": 0,
                               "offset": offset, "bytes": size, "path": str(profile.extflash),
                               "sha256": _hash_range(profile.extflash, offset, size)})
    if profile.resolved_sd and profile.resolved_sd.is_file():
        files = _sd_inventory(profile.resolved_sd)
        manifest = json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        candidates.append({"region": "sd", "kind": "sd-files-overlay", "offset": 0,
                           "bytes": sum(row["bytes"] for row in files), "files": files,
                           "path": str(profile.resolved_sd),
                           "sha256": hashlib.sha256(manifest).hexdigest()})

    extflash_limit = 256 * 1024 * 1024
    if profile.extflash.is_file():
        extflash_limit = min(extflash_limit, profile.extflash.stat().st_size)
    for row in candidates:
        if row.get("kind") != "external-flash":
            continue
        offset, end = int(row.get("offset", 0)), int(row.get("offset", 0)) + int(row["bytes"])
        if offset < 0 or end > extflash_limit:
            raise ValueError(
                f"{row['region']} range 0x{offset:X}–0x{end:X} exceeds the profile extflash limit "
                f"of 0x{extflash_limit:X}")

    selected = set(regions or (row["region"] for row in candidates))
    unknown = selected - {row["region"] for row in candidates}
    if unknown:
        raise ValueError("profile has no deployable region(s): " + ", ".join(sorted(unknown)))
    plan = [row for row in candidates if row["region"] in selected]
    return {"schemaVersion": 1, "profile": str(profile.root), "regions": plan,
            "bootAfterDeploy": "bank1 reset vector", "sdBehavior": "file overlay; existing files are retained"}


def deployment_write_summary(profile_path: str | Path, plan: dict) -> list[dict[str, str | int | None]]:
    """Describe known firmware content and absolute address ranges in a plan."""
    from .profiles import DeviceProfile

    profile = DeviceProfile.load(profile_path)
    metadata_path = profile.root / "provision.json"
    try:
        metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
    except (OSError, ValueError):
        metadata = {}
    firmware = metadata.get("firmware")
    retro_go_version = firmware.get("version") if isinstance(firmware, dict) else None
    provisioned_bootloader = metadata.get("bootloader")
    is_stock = firmware == "stock" or metadata.get("model") in {"mario", "zelda"}
    stock_model = str(metadata.get("model", "")).strip().title()
    patch_info = metadata.get("patch") or metadata.get("patches")
    boot_label = "Unknown"
    if provisioned_bootloader:
        if isinstance(provisioned_bootloader, dict):
            version = provisioned_bootloader.get("version")
            boot_label = "Bootloader" + (f" {version}" if version else "")
        else:
            boot_label = "Bootloader"
    elif is_stock:
        model_label = f" {stock_model}" if stock_model in {"Mario", "Zelda"} else ""
        patch_suffix = " (+patch)" if patch_info else ""
        if metadata.get("bootloaderInstalled") or metadata.get("bootloader") is True:
            patch_suffix = " (+patch,+bootloader)" if patch_info else " (+bootloader)"
        boot_label = "Stock" + model_label + patch_suffix

    rows: list[dict[str, str | int | None]] = []
    for row in plan.get("regions", []):
        region = row["region"]
        size = int(row.get("bytes", 0))
        if row.get("content"):
            content = str(row["content"])
            if region in {"bank1", "bank2"}:
                bank = int(row["bank"])
                address = 0x08000000 if bank == 1 else 0x08100000
                rows.append({"target": f"Bank {bank} (0x{address + int(row.get('offset', 0)):08x}–0x{address + int(row.get('offset', 0)) + size:08x})",
                             "content": content, "bytes": size, "sha256": row.get("sha256")})
            elif region in {"extflash", "frogfs", "littlefs"}:
                start = 0x90000000 + int(row.get("offset", 0))
                rows.append({"target": f"Extflash (0x{start:08x}–0x{start + size:08x})",
                             "content": content, "bytes": size, "sha256": row.get("sha256")})
            continue
        if region in {"bank1", "bank2"}:
            bank = int(row.get("bank", 1 if region == "bank1" else 2))
            address = 0x08000000 if bank == 1 else 0x08100000
            if bank == 1:
                content = boot_label
            elif retro_go_version:
                content = f"Retro-Go {retro_go_version}"
            elif is_stock:
                data = Path(row["path"]).read_bytes()
                content = "Erased (0xFF)" if data and all(byte == 0xFF for byte in data) else "Unknown"
            else:
                content = "Unknown"
            rows.append({"target": f"Bank {bank} (0x{address:08x}–0x{address + size:08x})",
                         "content": content, "bytes": size})
        elif region == "extflash":
            if is_stock and stock_model in {"Mario", "Zelda"}:
                content = f"{stock_model} Assets (profile image; target size read before writing)"
            elif is_stock:
                content = "Stock Assets (target size read before writing)"
            else:
                content = "External-flash image (unknown composition)"
            rows.append({"target": "Extflash (range read from device)",
                         "content": content, "bytes": None})
        elif region in {"frogfs", "littlefs"}:
            label = "Retro-Go FrogFS" if region == "frogfs" else "Retro-Go LittleFS"
            start = 0x90000000 + int(row.get("offset", 0))
            rows.append({"target": f"Extflash (0x{start:08x}–0x{start + size:08x})",
                         "content": label, "bytes": size})
        elif region == "sd":
            rows.append({"target": "SD image", "content": "File overlay", "bytes": size})
    return rows


def inspect_device_extflash(backend, gnw) -> dict:
    """Read the target's flash capacity and current firmware-declared layout."""
    size = int(gnw.external_flash_size)
    if size <= 0:
        raise RuntimeError("gnwmanager reported an invalid external-flash size")
    model = None
    try:
        from gnwmanager.cli.devices import AutodetectError, DeviceModel
        model = str(DeviceModel.autodetect(gnw)).title()
    except ImportError:
        pass
    except AutodetectError:
        pass

    layout = None
    layout_error = None
    for bank, address in ((1, 0x08000000), (2, 0x08100000)):
        try:
            image = backend.read_memory(address, 256 * 1024)
        except Exception as exc:
            layout_error = str(exc)
            continue
        matches = []
        for offset in range(0, len(image) - 35, 4):
            if (image[offset:offset + 8] == b"GWLB\x02\x00\x24\x00"
                    and zlib.crc32(image[offset:offset + 32])
                    == struct.unpack_from("<I", image, offset + 32)[0]):
                matches.append(struct.unpack_from("<6I", image, offset + 8))
        if len(matches) == 1:
            layout = {"bank": bank, "fields": matches[0]}
            break

    regions = []
    if layout:
        frog_start, frog_size, declared_size, _, littlefs_size, _ = layout["fields"]
        if frog_size:
            regions.append({"offset": frog_start, "bytes": frog_size,
                            "content": "FrogFS (current firmware layout)"})
        if littlefs_size:
            littlefs_offset = declared_size - littlefs_size
            regions.append({"offset": littlefs_offset, "bytes": littlefs_size,
                            "content": "LittleFS (current firmware layout)"})
        if not regions:
            regions.append({"offset": 0, "bytes": size,
                            "content": "Firmware declares no extflash filesystems"})
    elif model in {"Mario", "Zelda"}:
        regions.append({"offset": 0, "bytes": size, "content": f"{model} Assets (detected)"})
    else:
        content = "Current composition unknown"
        if layout_error:
            content += " (internal flash layout could not be read)"
        regions.append({"offset": 0, "bytes": size, "content": content})

    return {"size": size, "model": model,
            "regions": [{**row,
                         "target": (f"0x{0x90000000 + row['offset']:08x}–"
                                    f"0x{0x90000000 + row['offset'] + row['bytes']:08x}")}
                        for row in regions]}


def _sd_inventory(image: Path) -> list[dict]:
    """Validate a raw FAT image and return file-level deployment hashes."""
    with image.open("rb") as stream:
        header = stream.read(512)
    if header.startswith(b"QFI\xfb"):
        raise ValueError(f"SD source is QCOW2, not a raw card image: {image}; "
                         "close the VM and use the original profile sdcard.img")
    offsets = []
    if header[510:512] == b"\x55\xaa":
        for index in range(4):
            entry = header[446 + index * 16:462 + index * 16]
            if entry[4] in (1, 4, 6, 11, 12, 14):
                offsets.append(int.from_bytes(entry[8:12], "little") * 512)
    if not offsets:
        offsets = [0]
    try:
        from pyfatfs.PyFatFS import PyFatFS
    except ImportError as exc:
        raise RuntimeError('SD deployment needs `pip install "gwprov[device]"`') from exc

    entries = []
    for offset in offsets:
        try:
            with PyFatFS(str(image), offset=offset, read_only=True) as filesystem:
                for path, info in filesystem.walk.info(namespaces=["details"]):
                    if info.is_dir:
                        continue
                    digest = hashlib.sha256()
                    size = 0
                    with filesystem.openbin(path, "r") as source:
                        while True:
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            digest.update(chunk)
                            size += len(chunk)
                    entries.append({"path": path.lstrip("/"), "bytes": size,
                                    "sha256": digest.hexdigest()})
            break
        except Exception as error:
            if offset == offsets[-1]:
                raise ValueError(f"cannot inventory SD FAT image {image}: {error}") from error
    entries.sort(key=lambda row: row["path"].casefold())
    if not entries:
        raise ValueError(f"SD image contains no files: {image}")
    return entries


def _push_sd_image(gnw, image: Path) -> int:
    from pyfatfs.PyFatFS import PyFatFS
    with image.open("rb") as stream:
        mbr = stream.read(512)
    offsets = []
    if mbr[510:512] == b"\x55\xaa":
        for index in range(4):
            entry = mbr[446 + index * 16:462 + index * 16]
            if entry[4] in (1, 4, 6, 11, 12, 14):
                offsets.append(int.from_bytes(entry[8:12], "little") * 512)
    if not offsets:
        offsets = [0]
    count = 0
    for offset in offsets:
        opened = False
        try:
            with PyFatFS(str(image), offset=offset, read_only=True) as filesystem:
                opened = True
                for path, info in filesystem.walk.info(namespaces=["details"]):
                    if info.is_dir:
                        continue
                    with filesystem.openbin(path, "r") as source:
                        gnw.sd_write_file(path if path.startswith("/") else "/" + path,
                                          source.read())
                    count += 1
                return count
        except Exception:
            # An invalid partition can be skipped, but never continue with a
            # second partition after any device write has begun.
            if opened:
                raise
            if offset == offsets[-1]:
                raise
    raise ValueError(f"cannot open a FAT partition in SD image {image}")


def overlay_sd_directory(image: str | Path, destination: str | Path) -> int:
    """Copy files from a profile FAT image into an already-mounted card folder."""
    from pyfatfs.PyFatFS import PyFatFS

    image = Path(image)
    root = Path(destination).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"registered SD-card mount is unavailable: {root}")
    with image.open("rb") as stream:
        header = stream.read(512)
    if header.startswith(b"QFI\xfb"):
        raise ValueError(f"SD source is QCOW2, not a raw card image: {image}")
    offsets = []
    if header[510:512] == b"\x55\xaa":
        for index in range(4):
            entry = header[446 + index * 16:462 + index * 16]
            if entry[4] in (1, 4, 6, 11, 12, 14):
                offsets.append(int.from_bytes(entry[8:12], "little") * 512)
    if not offsets:
        offsets = [0]
    for offset in offsets:
        opened = False
        try:
            with PyFatFS(str(image), offset=offset, read_only=True) as filesystem:
                opened = True
                copied = 0
                for path, info in filesystem.walk.info(namespaces=["details"]):
                    if info.is_dir:
                        continue
                    relative = PurePosixPath(path.lstrip("/"))
                    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                        raise ValueError(f"unsafe path in SD image: {path}")
                    target = root.joinpath(*relative.parts)
                    parent = target.parent
                    parent.mkdir(parents=True, exist_ok=True)
                    if root not in parent.resolve().parents and parent.resolve() != root:
                        raise ValueError(f"SD-card path escapes mounted folder: {path}")
                    if target.is_symlink():
                        raise ValueError(f"refusing to overwrite SD-card symlink: {target}")
                    with filesystem.openbin(path, "r") as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    copied += 1
                if copied == 0:
                    raise ValueError(f"SD image contains no files: {image}")
                return copied
        except ValueError:
            raise
        except Exception as error:
            if opened:
                raise ValueError(f"cannot finish SD overlay from {image}: {error}") from error
            if offset == offsets[-1]:
                raise ValueError(f"cannot overlay SD FAT image {image}: {error}") from error
    raise ValueError(f"cannot open a FAT partition in SD image {image}")


def overlay_sd_tree(source: str | Path, destination: str | Path) -> int:
    """Copy project files into a registered mounted card, retaining unrelated files."""
    source_root = Path(source).expanduser().resolve()
    destination_root = Path(destination).expanduser().resolve()
    if not source_root.is_dir():
        raise ValueError(f"project SD content directory is unavailable: {source_root}")
    if not destination_root.is_dir():
        raise ValueError(f"registered SD-card mount is unavailable: {destination_root}")
    files = sorted((item for item in source_root.rglob("*") if item.is_file()),
                   key=lambda item: item.as_posix().casefold())
    if not files:
        raise ValueError("project bundle contains no SD files to install")
    for source_file in files:
        relative = source_file.relative_to(source_root)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe project SD path: {relative}")
        target = destination_root / relative
        cursor = destination_root
        for part in relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError(f"refusing to follow SD-card symlink: {cursor}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if destination_root not in target.parent.resolve().parents and target.parent.resolve() != destination_root:
            raise ValueError(f"SD-card path escapes mounted folder: {relative}")
        shutil.copyfile(source_file, target)
    return len(files)


def apply_deployment(profile_path: str | Path, *, probe_id: str | None = None,
                     programmer: str | None = None, remote_url: str | None = None,
                     remote_origin: str | None = None,
                     regions: Iterable[str] | None = None,
                     start: bool = False, bank: int = 1,
                     progress: Callable[[dict], None] | None = None) -> dict:
    """Write the selected plan; optionally start firmware from an internal bank."""
    if bank not in (1, 2):
        raise ValueError("start bank must be 1 or 2")
    selected = sum(bool(value) for value in (probe_id, programmer, remote_url))
    if selected > 1:
        raise ValueError("select one of --probe-id, --programmer, or --remote-url")
    plan = deployment_plan(profile_path, regions)
    if not plan["regions"]:
        raise ValueError("deployment plan is empty")

    if remote_url:
        from .backends import WebSocketBackend
        backend = WebSocketBackend(remote_url, origin=remote_origin,
                                   operation="gwprov deploy",
                                   allow_recovery_marker=True)
    elif probe_id:
        from .backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(probe_id, operation="gwprov deploy",
                                      allow_recovery_marker=True,
                                      resume_on_disconnect=False)
    elif programmer:
        from .backends import SelectedOpenOCDBackend
        backend = SelectedOpenOCDBackend(programmer, operation="gwprov deploy",
                                         allow_recovery_marker=True)
    else:
        from .backends import AutoOpenOCDBackend
        backend = AutoOpenOCDBackend(operation="gwprov deploy",
                                     allow_recovery_marker=True)

    from gnwmanager.gnw import GnW
    written = []
    overall_done = 0

    def report(event: str, **values) -> None:
        if progress is not None:
            progress({"event": event, **values})

    try:
        report("phase", message="Connecting to hardware")
        backend.open()
        from .hw_lifecycle import (assert_application_mode,
                                   attach_to_idle_gnwmanager,
                                   clear_backend_recovery_required,
                                   inspect_target_mode,
                                   mark_backend_flasher_ready,
                                   mark_backend_recovery_required,
                                   _reset_and_halt_for_stub,
                                   _resume_after_stub_transition,
                                   start_gnwmanager_verified)
        gnw = GnW(backend)
        mode = inspect_target_mode(backend)
        if mode["stubDetected"] and mode["stubAlive"]:
            report("phase", message="Attaching to gnwmanager RAM programmer")
            mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
            attach_to_idle_gnwmanager(backend, gnw)
        elif mode["stubDetected"]:
            raise RuntimeError(
                "Cannot deploy: a RAM service is resident but stopped. Check the target "
                "connection and retry after the service is running."
            )
        else:
            assert_application_mode(backend, operation="start a deployment")
            # This is intentionally the first mutating operation: it switches the
            # target into gnwmanager's RAM service to perform explicit region writes.
            report("phase", message="Starting gnwmanager RAM programmer")
            mark_backend_recovery_required(backend, "starting gnwmanager RAM programmer")
            start_gnwmanager_verified(backend, gnw)
            mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
        report("phase", message="Reading device extflash layout")
        extflash = inspect_device_extflash(backend, gnw)
        report("device_extflash", summary=extflash)
        for row in plan["regions"]:
            if row.get("kind") == "external-flash":
                end = int(row.get("offset", 0)) + int(row.get("bytes", 0))
                if end > extflash["size"]:
                    raise ValueError(
                        f"profile {row['region']} ends at extflash offset 0x{end:x}, "
                        f"but the device has only {_human_size(extflash['size'])}; "
                        "no flash regions were written"
                    )
        total_bytes = sum(int(row.get("bytes", 0)) for row in plan["regions"])
        report("start", total_bytes=total_bytes, region_count=len(plan["regions"]))
        for row in plan["regions"]:
            region = row["region"]
            path = Path(row["path"])
            region_total = int(row.get("bytes", 0))
            region_done = 0
            report("region_start", region=region, bytes=region_total,
                   overall_done=overall_done, overall_total=total_bytes)
            # gnwmanager's LCD progress field is a 0..26 counter. Reset it for
            # each component; flash() advances it per external-flash packet.
            gnw.write_uint32("progress", 0)
            if row["kind"] == "internal-flash":
                gnw.flash(row["bank"], row["offset"], path.read_bytes())
                region_done = region_total
                overall_done += region_total
                gnw.write_uint32("progress", 26)
                report("progress", region=region, region_done=region_done,
                       region_total=region_total, overall_done=overall_done,
                       overall_total=total_bytes)
                written.append({"region": region, "bytes": row["bytes"],
                                "sha256": row["sha256"]})
            elif region == "sd":
                count = _push_sd_image(gnw, path)
                region_done = region_total
                overall_done += region_total
                gnw.write_uint32("progress", 26)
                report("progress", region=region, region_done=region_done,
                       region_total=region_total, overall_done=overall_done,
                       overall_total=total_bytes)
                written.append({"region": region, "files": count, "mode": "overlay"})
            else:
                with path.open("rb") as stream:
                    stream.seek(int(row.get("sourceOffset", row["offset"])))
                    data = stream.read(region_total)
                if len(data) != region_total:
                    raise ValueError(f"truncated {region} image")
                # Let gnwmanager see the whole mapped component at once so its
                # device-side progress denominator covers all 256 KiB packets.
                # Calling flash() once per packet would make every packet look
                # like a complete operation on the LCD.
                gnw.flash(0, row["offset"], data)
                gnw.write_uint32("progress", 26)
                region_done = region_total
                overall_done += region_total
                report("progress", region=region, region_done=region_done,
                       region_total=region_total, overall_done=overall_done,
                       overall_total=total_bytes)
                written.append({"region": region, "bytes": row["bytes"],
                                "sha256": row["sha256"]})
            report("region_complete", region=region, region_done=region_done,
                   region_total=region_total, overall_done=overall_done,
                   overall_total=total_bytes)
        booted = None
        if start:
            base = 0x08000000 if bank == 1 else 0x08100000
            report("phase", message=f"Starting from bank {bank}")
            watchdog_freezes = _reset_and_halt_for_stub(backend, gnw)
            backend.write_register("msp", backend.read_uint32(base))
            backend.write_register("pc", backend.read_uint32(base + 4))
            _resume_after_stub_transition(backend, watchdog_freezes)
            state = inspect_target_mode(backend)
            if state["halted"] or state["stubResident"]:
                raise RuntimeError(
                    f"deployment wrote the selected regions, but bank {bank} did not start "
                    "(target still halted or VTOR remains in SRAM)."
                )
            clear_backend_recovery_required(backend)
            booted = f"bank{bank}"
        else:
            # This branch intentionally leaves the RAM service running. It may
            # retain a flash VTOR, so use the builder's mailbox + running-core
            # liveness check for this explicit post-write verification.
            state = inspect_target_mode(backend)
            if not state["stubDetected"] or not state["stubAlive"]:
                raise RuntimeError(
                    "deployment wrote the selected regions, but the RAM flasher is no "
                    "longer running and idle"
                )
            mark_backend_flasher_ready(backend)
        report("complete", overall_done=overall_done, overall_total=total_bytes,
               booted=booted)
    finally:
        backend.close()
    return {**plan, "written": written, "booted": booted}
