"""Read and decode Retro-Go's stored /CONFIG for the active GWProv device."""
from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

CONFIG_FORMAT = "<I12B256s3H96sBxI32sI"
CONFIG_SIZE = struct.calcsize(CONFIG_FORMAT)
EXTERNAL_FLASH_BASE = 0x90000000


def decode_config(blob: bytes) -> dict:
    """Decode the currently supported persistent_config_t layout."""
    from .common.retrogo_config import CONFIG_MAGIC, CONFIG_VERSION

    if len(blob) != CONFIG_SIZE:
        raise ValueError(f"/CONFIG is {len(blob)} bytes; expected {CONFIG_SIZE}")
    values = struct.unpack(CONFIG_FORMAT, blob)
    magic, version = values[:2]
    startup_file = values[13].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    browse_subpath = values[17].split(b"\0", 1)[0].decode("utf-8", errors="replace")
    reserved_app = values[20]
    stored_crc = values[21]
    computed_crc = zlib.crc32(blob[:-4] + b"\0\0\0\0") & 0xFFFFFFFF
    decoded = {
        "version": version,
        "backlight": values[2],
        "start_action": values[3],
        "volume": values[4],
        "font_size": values[5],
        "theme": values[6],
        "colors": values[7],
        "turbo_buttons": values[8],
        "font": values[9],
        "language": values[10],
        "startup_app": values[11],
        "cpu_oc_level": values[12],
        "startup_file": startup_file,
        "main_menu_timeout_s": values[14],
        "selected_tab": values[15],
        "cursor": values[16],
        "browse_subpath": browse_subpath,
        "debug_clock_always_on": bool(values[18]),
        "welcome_prompt": values[19],
    }
    if any(reserved_app):
        decoded["reserved_app_hex"] = reserved_app.hex()
    integrity = {
        "magic_valid": magic == CONFIG_MAGIC,
        "version_supported": version == CONFIG_VERSION,
        "crc_valid": stored_crc == computed_crc,
        "stored_crc32": f"0x{stored_crc:08x}",
        "computed_crc32": f"0x{computed_crc:08x}",
    }
    return {"values": decoded, "integrity": integrity}


class _ImageContext:
    def __init__(self, stream, start: int, size: int, block_size: int):
        self.stream = stream
        self.start = start
        self.size = size
        self.block_size = block_size

    def read(self, cfg, block: int, off: int, size: int) -> bytearray:
        position = self.start + self.size - (block + 1) * self.block_size + off
        self.stream.seek(position)
        return bytearray(self.stream.read(size))

    def prog(self, cfg, block: int, off: int, data: bytes) -> int:
        raise OSError("device CONFIG inspection is read-only")

    def erase(self, cfg, block: int) -> int:
        raise OSError("device CONFIG inspection is read-only")

    def sync(self, cfg) -> int:
        return 0


class _MemoryContext:
    def __init__(self, backend, start: int, size: int, block_size: int):
        self.backend = backend
        self.start = start
        self.size = size
        self.block_size = block_size
        self.cache: dict[int, bytes] = {}

    def read(self, cfg, block: int, off: int, size: int) -> bytearray:
        data = self.cache.get(block)
        if data is None:
            position = self.start + self.size - (block + 1) * self.block_size
            data = self.backend.read_memory(EXTERNAL_FLASH_BASE + position,
                                            self.block_size)
            self.cache[block] = data
        return bytearray(data[off:off + size])

    def prog(self, cfg, block: int, off: int, data: bytes) -> int:
        raise OSError("device CONFIG inspection is read-only")

    def erase(self, cfg, block: int) -> int:
        raise OSError("device CONFIG inspection is read-only")

    def sync(self, cfg) -> int:
        return 0


def _layout(profile_value: str):
    from .profiles import DeviceProfile

    profile = DeviceProfile.load(profile_value)
    metadata_path = profile.root / "provision.json"
    if not metadata_path.is_file():
        raise ValueError(f"profile has no provision.json layout: {profile.root}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    layout = metadata.get("layout", {})
    try:
        extflash_size = int(layout["extflashBytes"])
        start = int(layout["littlefsOffset"])
        size = int(layout["littlefsBytes"])
        block_size = int(metadata.get("firmware", {}).get("littlefsBlockSize", 4096))
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("profile does not declare a usable LittleFS partition layout") from error
    if start < 0 or size <= 0 or block_size <= 0 or start + size > extflash_size:
        raise ValueError("profile declares an invalid LittleFS partition layout")
    if size % block_size:
        raise ValueError("LittleFS size is not a whole number of blocks")
    return profile, extflash_size, start, size, block_size


def _read_littlefs(context, size: int, block_size: int) -> bytes:
    try:
        from littlefs import LittleFS
    except ImportError as error:
        raise RuntimeError("CONFIG inspection requires the littlefs package") from error
    fs = LittleFS(context=context, mount=False, block_size=block_size,
                  block_count=size // block_size)
    mounted = False
    try:
        fs.mount()
        mounted = True
        with fs.open("/CONFIG", "rb") as source:
            return source.read()
    except FileNotFoundError as error:
        raise ValueError("the active device's LittleFS does not contain /CONFIG") from error
    finally:
        if mounted:
            fs.unmount()


def _vm_image(device_id: str):
    import psutil
    from .gwemu_manager import instances

    try:
        pid = int(device_id.split(":", 1)[1])
    except (IndexError, ValueError) as error:
        raise ValueError(f"invalid GWemu device ID: {device_id}") from error
    instance = next((row for row in instances() if row.get("pid") == pid), None)
    if instance is None:
        raise ValueError(f"active GWemu device {device_id} is no longer running")
    profile_value = instance.get("profile")
    if not profile_value or profile_value == "raw image":
        raise ValueError("this GWemu instance has no managed profile layout for LittleFS")
    profile, _, start, size, block_size = _layout(profile_value)
    process = psutil.Process(pid)
    cwd = Path(process.cwd())
    extflash_path = None
    for argument in process.cmdline():
        marker = "gnw-h7b0-soc.extflash-image="
        if marker in argument:
            value = argument.split(marker, 1)[1]
            path = Path(value).expanduser()
            extflash_path = (path if path.is_absolute() else cwd / path).resolve()
            break
    if extflash_path is None:
        extflash_path = profile.extflash
    if not extflash_path.is_file():
        raise FileNotFoundError(f"GWemu extflash image is missing: {extflash_path}")
    if extflash_path.stat().st_size < start + size:
        raise ValueError("GWemu extflash image is smaller than its declared LittleFS partition")
    with extflash_path.open("rb") as stream:
        blob = _read_littlefs(_ImageContext(stream, start, size, block_size),
                              size, block_size)
    return blob, f"GWemu extflash image: {extflash_path}"


def _hardware_blob(device_id: str) -> tuple[bytes, str]:
    from .active_device import get_active, get_active_origin
    from .backends import SelectedPyOCDBackend, WebSocketBackend
    from .device_assignments import get_assignment

    assigned = get_assignment(device_id).get("profile")
    if not assigned:
        raise ValueError("assign a device profile first with `gwprov set profile PROFILE`; "
                         "GWProv needs its LittleFS layout to locate /CONFIG")
    profile, extflash_size, start, size, block_size = _layout(assigned)
    if device_id.startswith("probe:"):
        unique_id = device_id.removeprefix("probe:")
        backend = SelectedPyOCDBackend(unique_id, operation="gwprov config read",
                                       lease_wait=0)
    elif device_id.startswith("remote:"):
        uri = device_id.removeprefix("remote:")
        origin = get_active_origin() if device_id == get_active() else None
        backend = WebSocketBackend(uri, origin=origin,
                                   operation="gwprov config read", lease_wait=0)
    else:
        raise ValueError(f"unsupported hardware device ID: {device_id}")
    try:
        backend.open()
        context = _MemoryContext(backend, start, size, block_size)
        blob = _read_littlefs(context, size, block_size)
    finally:
        backend.close()
    return blob, f"hardware mapped flash using profile layout: {profile.root}"


def show_device_config(*, output: str = "text") -> int:
    from .active_device import get_active

    device_id = get_active()
    if not device_id:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    if device_id.startswith("gwemu:"):
        blob, source = _vm_image(device_id)
    else:
        blob, source = _hardware_blob(device_id)
    decoded = decode_config(blob)
    report = {"schemaVersion": 1, "device": device_id, "source": source,
              "path": "/CONFIG", "bytes": len(blob), **decoded}
    valid = all(decoded["integrity"][key]
                for key in ("magic_valid", "version_supported", "crc_valid"))
    if output == "json":
        print(json.dumps(report, indent=2))
    else:
        from rich.console import Console
        from rich.table import Table
        from rich.text import Text

        console = Console()
        table = Table(title=f"Retro-Go /CONFIG · {device_id}", title_justify="left",
                      expand=True)
        table.add_column("SETTING", style="cyan", overflow="fold")
        table.add_column("VALUE", overflow="fold")
        for key, value in decoded["values"].items():
            display = value
            if key == "start_action":
                label = {0: "resume", 1: "new game"}.get(value, "unknown")
                display = f"{label} ({value})"
            table.add_row(key.replace("_", " "), str(display))
        console.print(table)
        checks = decoded["integrity"]
        integrity = Text("CONFIG integrity: ", style="bold")
        integrity.append("valid" if valid else "invalid",
                         style="green" if valid else "red")
        integrity.append(f" · CRC {checks['stored_crc32']}")
        console.print(integrity)
        console.print(f"Source: {source}", style="dim")
    return 0 if valid else 1
