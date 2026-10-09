"""Plan and apply profile deployment to hardware through gnwmanager."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from pathlib import PurePosixPath
import shutil
from typing import Iterable

CHUNK_SIZE = 256 * 1024


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

    for name, path, bank in (("bank1", profile.bank1, 1), ("bank2", profile.bank2, 2)):
        if path.is_file():
            size = path.stat().st_size
            if size > 256 * 1024:
                raise ValueError(f"{name} image is larger than an internal flash bank")
            candidates.append({"region": name, "kind": "internal-flash", "bank": bank,
                               "offset": 0, "bytes": size, "path": str(path),
                               "sha256": _hash_range(path, 0, size)})

    if metadata.get("firmware") == "stock" or metadata.get("model") in {"mario", "zelda"}:
        if profile.extflash.is_file():
            size = profile.extflash.stat().st_size
            candidates.append({"region": "extflash", "kind": "external-flash", "bank": 0,
                               "offset": 0, "bytes": size, "path": str(profile.extflash),
                               "sha256": _hash_range(profile.extflash, 0, size)})
    else:
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

    selected = set(regions or (row["region"] for row in candidates))
    unknown = selected - {row["region"] for row in candidates}
    if unknown:
        raise ValueError("profile has no deployable region(s): " + ", ".join(sorted(unknown)))
    plan = [row for row in candidates if row["region"] in selected]
    return {"schemaVersion": 1, "profile": str(profile.root), "regions": plan,
            "bootAfterDeploy": "bank1 reset vector", "sdBehavior": "file overlay; existing files are retained"}


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


def apply_deployment(profile_path: str | Path, *, probe_id: str | None = None,
                     programmer: str | None = None, remote_url: str | None = None,
                     remote_origin: str | None = None,
                     regions: Iterable[str] | None = None) -> dict:
    """Write the selected deployment plan and boot bank 1 afterwards."""
    selected = sum(bool(value) for value in (probe_id, programmer, remote_url))
    if selected > 1:
        raise ValueError("select one of --probe-id, --programmer, or --remote-url")
    plan = deployment_plan(profile_path, regions)
    if not plan["regions"]:
        raise ValueError("deployment plan is empty")

    if remote_url:
        from .backends import WebSocketBackend
        backend = WebSocketBackend(remote_url, origin=remote_origin,
                                   operation="gwprov deploy")
    elif probe_id:
        from .backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(probe_id, operation="gwprov deploy")
    elif programmer:
        from .backends import SelectedOpenOCDBackend
        backend = SelectedOpenOCDBackend(programmer, operation="gwprov deploy")
    else:
        from .backends import AutoOpenOCDBackend
        backend = AutoOpenOCDBackend(operation="gwprov deploy")

    from gnwmanager.gnw import GnW
    written = []
    try:
        backend.open()
        gnw = GnW(backend)
        # This is intentionally the first mutating operation: it switches the
        # target into gnwmanager's RAM service to perform explicit region writes.
        gnw.start_gnwmanager()
        for row in plan["regions"]:
            region = row["region"]
            path = Path(row["path"])
            if row["kind"] == "internal-flash":
                gnw.flash(row["bank"], row["offset"], path.read_bytes())
                written.append({"region": region, "bytes": row["bytes"],
                                "sha256": row["sha256"]})
            elif region == "sd":
                count = _push_sd_image(gnw, path)
                written.append({"region": region, "files": count, "mode": "overlay"})
            else:
                offset, remaining = row["offset"], row["bytes"]
                with path.open("rb") as stream:
                    stream.seek(offset)
                    while remaining:
                        data = stream.read(min(CHUNK_SIZE, remaining))
                        if not data:
                            raise ValueError(f"truncated {region} image")
                        gnw.flash(0, offset, data)
                        offset += len(data)
                        remaining -= len(data)
                written.append({"region": region, "bytes": row["bytes"],
                                "sha256": row["sha256"]})
        # Match `gnwmanager start bank1`: reset, load MSP/PC from the bank-1
        # vector table, then resume. Bank 1 is therefore a real deployable part
        # of every profile, including stock OFW and bootloader payloads.
        backend.reset_and_halt()
        backend.write_register("msp", backend.read_uint32(0x08000000))
        backend.write_register("pc", backend.read_uint32(0x08000004))
        backend.resume()
    finally:
        backend.close()
    return {**plan, "written": written, "booted": "bank1"}
