"""GWProv-owned RAM recovery-stub startup without a GnW client instance."""

from __future__ import annotations

import hashlib
import struct as _struct
import time
from importlib.resources import files
from pathlib import Path as _Path

from . import hw_lifecycle as lifecycle

RAM_CHUNK_BYTES = 4096
SRAM_DTCM_START = 0x20000000
SRAM_DTCM_END = 0x20020000
WATCHDOG_REGISTERS = (lifecycle.DBGMCU_APB3FZ1, lifecycle.DBGMCU_APB4FZ1)
STM32H7B0_UID_BASE = 0x08FFF800
STM32H7B0_UID_BYTES = 12


class RamRecoveryError(RuntimeError):
    """Recovery failed; ``receipt`` describes the observed fail-closed state."""

    def __init__(self, message: str, receipt: dict):
        super().__init__(message)
        self.receipt = receipt


def _ram_flasher_image() -> bytes:
    """Load the packaged recovery image; this does not construct a GnW client."""
    return (files("gnwmanager") / "firmware.bin").read_bytes()


def _validate_image(image: bytes) -> tuple[int, int]:
    start = lifecycle.GNWMANAGER_FIRMWARE_ADDR
    end = start + len(image)
    if len(image) < 8 or end > lifecycle.RAM_STUB_END:
        raise lifecycle.TargetModeError("packaged RAM recovery image has invalid geometry")
    initial_sp = int.from_bytes(image[:4], "little")
    initial_pc = int.from_bytes(image[4:8], "little")
    pc = initial_pc & ~1
    if not (SRAM_DTCM_START < initial_sp <= SRAM_DTCM_END and initial_sp % 8 == 0):
        raise lifecycle.TargetModeError("packaged RAM recovery MSP is not aligned within SRAM")
    if not (lifecycle.RAM_STUB_START <= pc < end and initial_pc & 1):
        raise lifecycle.TargetModeError("packaged RAM recovery vectors are outside the image")
    return initial_sp, initial_pc


def _write_image_verified(backend, image: bytes) -> int:
    start = lifecycle.GNWMANAGER_FIRMWARE_ADDR
    chunks = 0
    for offset in range(0, len(image), RAM_CHUNK_BYTES):
        chunk = image[offset : offset + RAM_CHUNK_BYTES]
        address = start + offset
        backend.write_memory(address, chunk)
        if backend.read_memory(address, len(chunk)) != chunk:
            raise lifecycle.TargetModeError(
                f"RAM recovery image readback differs at offset 0x{offset:x}"
            )
        chunks += 1
    return chunks


def _set_vectors(backend, image: bytes) -> tuple[int, int]:
    initial_sp, initial_pc = _validate_image(image)
    backend.write_register("msp", initial_sp)
    backend.write_register("pc", initial_pc)
    actual_pc = backend.read_register("pc") & 0xFFFFFFFF
    if (backend.read_register("msp") & 0xFFFFFFFF) != initial_sp or (actual_pc & ~1) != (
        initial_pc & ~1
    ):
        raise lifecycle.TargetModeError("RAM recovery vector registers failed readback")
    register_numbers = getattr(backend, "register_numbers", {})
    if isinstance(register_numbers, dict) and "xpsr" in register_numbers:
        xpsr = backend.read_register("xpsr") & 0xFFFFFFFF
        if not xpsr & (1 << 24):
            raise lifecycle.TargetModeError("Cortex-M xPSR.T is clear for the Thumb RAM vector")
    return initial_sp, initial_pc


def _wait_live_idle(backend, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = lifecycle.inspect_target_mode(backend)
        if (
            state["stubResident"]
            and state["stubAlive"]
            and state["serviceState"] == "idle"
            and not state["halted"]
        ):
            counter_a = backend.read_uint32(lifecycle.GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            time.sleep(0.05)
            confirmed = lifecycle.inspect_target_mode(backend)
            counter_b = backend.read_uint32(lifecycle.GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            if (
                confirmed["stubResident"]
                and confirmed["stubAlive"]
                and confirmed["serviceState"] == "idle"
                and not confirmed["halted"]
                and counter_a != 0
                and counter_a == counter_b
            ):
                return confirmed
        time.sleep(0.02)
    raise lifecycle.TargetModeError(
        "RAM recovery image did not reach stable live IDLE before timeout"
    )


def _launch_and_wait(backend, image: bytes, timeout: float) -> tuple[dict, int]:
    chunks = _write_image_verified(backend, image)
    backend.write_uint32(lifecycle.MAILBOX_STATUS, 0)
    backend.write_uint32(lifecycle.MAILBOX_STATUS + 4, 0)
    _set_vectors(backend, image)
    backend.resume()
    return _wait_live_idle(backend, timeout), chunks


def _is_halted(backend) -> bool | None:
    try:
        return bool(int(backend.read_uint32(lifecycle.DHCSR)) & lifecycle.S_HALT)
    except Exception:
        return None


def _best_effort_idle(
    backend, image: bytes, timeout: float, watchdog_freezes: dict, watchdog_before: dict
) -> dict:
    """Recover to live IDLE when possible; otherwise retain a precise receipt."""
    try:
        state = lifecycle.inspect_target_mode(backend)
    except Exception as error:
        state = {"inspection_error": str(error)}
    if (
        state.get("stubResident")
        and state.get("stubAlive")
        and state.get("serviceState") == "idle"
        and state.get("halted") is False
    ):
        lifecycle._restore_h7_watchdogs(backend, watchdog_freezes)
        watchdog_after = _watchdog_state(backend)
        return {
            "recovery_state": "live_idle",
            "state": lifecycle.inspect_target_mode(backend),
            "watchdog_freezes_restored": watchdog_after == watchdog_before,
            "watchdog_state_after": watchdog_after,
        }

    halted = _is_halted(backend)
    if halted is not True:
        try:
            backend.halt()
        except Exception:
            pass
        halted = _is_halted(backend)
    if halted is True:
        try:
            state, chunks = _launch_and_wait(backend, image, timeout)
            lifecycle._restore_h7_watchdogs(backend, watchdog_freezes)
            watchdog_after = _watchdog_state(backend)
            return {
                "recovery_state": "live_idle_after_reload",
                "state": state,
                "ram_chunks": chunks,
                "watchdog_freezes_restored": watchdog_after == watchdog_before,
                "watchdog_state_after": watchdog_after,
            }
        except Exception as error:
            try:
                state = lifecycle.inspect_target_mode(backend)
            except Exception as inspect_error:
                state = {"inspection_error": str(inspect_error)}
            return {
                "recovery_state": "core_halted_fail_closed",
                "state": state,
                "core_halted": _is_halted(backend),
                "error": str(error),
                "watchdog_freezes_restored": False,
                "watchdog_freezes_left_for_halted_core": {
                    hex(address): {"original": before, "mask": mask}
                    for address, (before, mask) in watchdog_freezes.items()
                },
            }
    return {
        "recovery_state": "unknown_core_state",
        "state": state,
        "core_halted": halted,
        "watchdog_freezes_restored": False,
        "watchdog_freezes": {
            hex(address): {"original": before, "mask": mask}
            for address, (before, mask) in watchdog_freezes.items()
        },
    }


def _watchdog_state(backend) -> dict:
    values = {}
    for address in WATCHDOG_REGISTERS:
        try:
            values[hex(address)] = int(backend.read_uint32(address)) & 0xFFFFFFFF
        except Exception as error:
            values[hex(address)] = f"unreadable: {error}"
    return values


def start_recovery_stub_ram_only(backend, *, timeout: float = 10.0) -> dict:
    """Reset an idle RAM stub and relaunch its verified RAM image.

    The target must already be running an idle recovery stub. The operation
    resets the MCU, writes only volatile RAM and core/debug registers, then
    requires the restarted stub to return to stable live IDLE. It never writes
    flash and never creates a ``GnW`` instance.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    before = lifecycle.inspect_target_mode(backend)
    if not (
        before["stubResident"]
        and before["stubAlive"]
        and before["serviceState"] == "idle"
        and not before["halted"]
    ):
        raise lifecycle.TargetModeError(
            "RAM-only fresh restart requires an already-running idle RAM recovery stub"
        )

    uid_before = bytes(backend.read_memory(STM32H7B0_UID_BASE, STM32H7B0_UID_BYTES))
    if len(uid_before) != STM32H7B0_UID_BYTES or uid_before in (b"\0" * 12, b"\xff" * 12):
        raise lifecycle.TargetModeError("device UID is unavailable before RAM-only restart")

    image = _ram_flasher_image()
    initial_sp, _ = _validate_image(image)
    watchdog_before = _watchdog_state(backend)
    watchdog_freezes = lifecycle._freeze_h7_watchdogs(backend)
    reset_observed_at_ns = None
    try:
        backend.reset_and_halt()
        reset_observed_at_ns = time.time_ns()
        dhcsr = int(backend.read_uint32(lifecycle.DHCSR)) & 0xFFFFFFFF
        if not dhcsr & lifecycle.S_HALT:
            raise lifecycle.TargetModeError(
                "fresh reset did not leave the core halted; no RAM image was written"
            )
        state, chunks = _launch_and_wait(backend, image, timeout)
        uid_after = bytes(backend.read_memory(STM32H7B0_UID_BASE, STM32H7B0_UID_BYTES))
        if uid_after != uid_before:
            raise lifecycle.TargetModeError("device UID changed across RAM-only restart")
        lifecycle._restore_h7_watchdogs(backend, watchdog_freezes)
        watchdog_after = _watchdog_state(backend)
        if watchdog_after != watchdog_before:
            raise lifecycle.TargetModeError("watchdog freeze state did not restore exactly")
        counter = backend.read_uint32(lifecycle.GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
        return {
            **state,
            "started": True,
            "contextCounter": counter,
            "device_uid": "stm32h7b0-uid:" + uid_after.hex(),
            "reset_observed_at_ns": reset_observed_at_ns,
            "ram_image_start": lifecycle.GNWMANAGER_FIRMWARE_ADDR,
            "ram_image_bytes": len(image),
            "ram_image_chunks": chunks,
            "ram_image_chunk_bytes_max": RAM_CHUNK_BYTES,
            "ram_image_sha256": hashlib.sha256(image).hexdigest(),
            "watchdog_freezes_restored": watchdog_after == watchdog_before,
            "watchdog_state_before": watchdog_before,
            "watchdog_state_after": watchdog_after,
            "persistent_flash_write_performed": False,
        }
    except Exception as error:
        recovery = _best_effort_idle(backend, image, timeout, watchdog_freezes, watchdog_before)
        receipt = {
            "started": False,
            "reset_observed_at_ns": reset_observed_at_ns,
            "persistent_flash_write_performed": False,
            "watchdog_state_before": watchdog_before,
            "watchdog_state_after": _watchdog_state(backend),
            "original_error": f"{type(error).__name__}: {error}",
            **recovery,
        }
        raise RamRecoveryError("RAM recovery restart failed", receipt) from error


STM32H7B0_UID_BASE = 0x08FFF800
STM32H7B0_UID_BYTES = 12
RESET_FLASH_VTOR = 0x08000000


def resume_halted_ram_stub(backend, *, expected_uid: str, timeout: float = 10.0) -> dict:
    """Resume only a verified halted post-reset RAM image into the recovery stub.

    This narrow repair path performs no reset, memory/register clearing, watchdog
    changes, or flash writes. It requires a halted core at reset-flash VTOR, an
    exact device UID match, and a byte-for-byte/hash match of the packaged RAM
    flasher before setting MSP/PC and resuming.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    image = _ram_flasher_image()
    initial_sp, initial_pc = _validate_image(image)
    actual_uid_raw = backend.read_memory(STM32H7B0_UID_BASE, STM32H7B0_UID_BYTES)
    if (
        not isinstance(actual_uid_raw, (bytes, bytearray, memoryview))
        or len(actual_uid_raw) != STM32H7B0_UID_BYTES
    ):
        raise lifecycle.TargetModeError("device UID readback is unavailable or short")
    actual_uid = "stm32h7b0-uid:" + bytes(actual_uid_raw).hex()
    if actual_uid != expected_uid:
        raise lifecycle.TargetModeError(
            "device UID differs from requested halted-stub recovery identity"
        )
    dhcsr = int(backend.read_uint32(lifecycle.DHCSR)) & 0xFFFFFFFF
    vtor = int(backend.read_uint32(lifecycle.VTOR)) & 0xFFFFFFFF
    status = int(backend.read_uint32(lifecycle.MAILBOX_STATUS)) & 0xFFFFFFFF
    if not dhcsr & lifecycle.S_HALT:
        raise lifecycle.TargetModeError("halted RAM-stub repair requires DHCSR.S_HALT")
    if vtor != RESET_FLASH_VTOR or status != 0:
        raise lifecycle.TargetModeError(
            "halted RAM-stub repair requires reset-flash VTOR and empty mailbox"
        )
    ram_image = backend.read_memory(lifecycle.GNWMANAGER_FIRMWARE_ADDR, len(image))
    image_sha256 = hashlib.sha256(image).hexdigest()
    if ram_image != image or hashlib.sha256(ram_image).hexdigest() != image_sha256:
        raise lifecycle.TargetModeError("halted RAM image differs from packaged recovery image")
    _set_vectors(backend, image)
    backend.resume()
    state = _wait_live_idle(backend, timeout)
    return {
        **state,
        "resumed": True,
        "device_uid": actual_uid,
        "ram_image_start": lifecycle.GNWMANAGER_FIRMWARE_ADDR,
        "ram_image_bytes": len(image),
        "ram_image_sha256": image_sha256,
        "persistent_flash_write_performed": False,
    }


# Running campaign RAM payload recovery is separate from the idle-stub restart
# above: it first proves the live image identity, then replaces it through the
# caller-owned GWProv session only.

_RAM_PAYLOAD_RANGES = (
    (0x00000000, 0x00010000),
    (0x20000000, 0x20020000),
    (0x24000000, 0x24100000),
    (0x30000000, 0x30020000),
)
_RAM_PAYLOAD_MAX_ELF = 2 * 1024 * 1024
_RAM_PAYLOAD_MAX_LOAD = 1024 * 1024
_RAM_PAYLOAD_MAX_SEGMENTS = 32
_RAM_PAYLOAD_PF_X = 1
_RAM_PAYLOAD_UID = STM32H7B0_UID_BASE
_RAM_PAYLOAD_VTOR = lifecycle.VTOR
_RAM_PAYLOAD_DHCSR = lifecycle.DHCSR


class RunningRamPayloadError(RuntimeError):
    """A running RAM image could not be safely replaced by the recovery stub."""


def _payload_inside(address: int, size: int) -> bool:
    if type(address) is not int or type(size) is not int or size <= 0:
        return False
    end = address + size
    return (
        end > address
        and end <= 0x100000000
        and any(start <= address and end <= limit for start, limit in _RAM_PAYLOAD_RANGES)
    )


def _parse_running_ram_elf(raw: bytes, vector_address: int):
    """Parse the strict ELF32 ARM subset needed to admit a live RAM image."""
    if type(raw) is not bytes or not 52 <= len(raw) <= _RAM_PAYLOAD_MAX_ELF:
        raise RunningRamPayloadError("RAM ELF size is outside accepted bounds")
    if raw[:7] != b"\x7fELF\x01\x01\x01":
        raise RunningRamPayloadError("RAM ELF must be ELF32 little-endian")
    e_type, machine = _struct.unpack_from("<HH", raw, 16)
    version = _struct.unpack_from("<I", raw, 20)[0]
    entry = _struct.unpack_from("<I", raw, 24)[0]
    phoff = _struct.unpack_from("<I", raw, 28)[0]
    ehsize, phentsize, phnum = _struct.unpack_from("<HHH", raw, 40)
    if (
        e_type != 2
        or machine != 40
        or version != 1
        or ehsize < 52
        or phentsize < 32
        or not 0 < phnum <= _RAM_PAYLOAD_MAX_SEGMENTS
        or phoff + phentsize * phnum > len(raw)
    ):
        raise RunningRamPayloadError("RAM ELF header or program-header table is invalid")
    segments = []
    virtual_extents, physical_extents = [], []
    total = 0
    for index in range(phnum):
        typ, offset, vaddr, paddr, filesz, memsz, flags, _align = _struct.unpack_from(
            "<8I", raw, phoff + index * phentsize
        )
        if typ != 1:
            continue
        if filesz > memsz or offset + filesz > len(raw):
            raise RunningRamPayloadError(f"PT_LOAD {index} has invalid sizes")
        if memsz and not _payload_inside(vaddr, memsz):
            raise RunningRamPayloadError(f"PT_LOAD {index} virtual extent is outside SRAM")
        if filesz and not _payload_inside(paddr, filesz):
            raise RunningRamPayloadError(f"PT_LOAD {index} physical extent is outside SRAM")
        if not any(start <= vaddr < limit for start, limit in _RAM_PAYLOAD_RANGES):
            raise RunningRamPayloadError(f"PT_LOAD {index} virtual address is outside SRAM")
        if not any(start <= paddr < limit for start, limit in _RAM_PAYLOAD_RANGES):
            raise RunningRamPayloadError(f"PT_LOAD {index} physical address is outside SRAM")
        if memsz > _RAM_PAYLOAD_MAX_LOAD:
            raise RunningRamPayloadError(f"PT_LOAD {index} exceeds memory limit")
        data = raw[offset : offset + filesz]
        if filesz:
            total += filesz
            physical_extents.append((paddr, paddr + filesz, index))
        if memsz:
            virtual_extents.append((vaddr, vaddr + memsz, index))
        segments.append(
            {
                "index": index,
                "vaddr": vaddr,
                "paddr": paddr,
                "filesz": filesz,
                "memsz": memsz,
                "flags": flags,
                "data": data,
            }
        )
    if not segments or not total or total > _RAM_PAYLOAD_MAX_LOAD:
        raise RunningRamPayloadError("RAM ELF has no bounded file-backed load data")
    for extents, label in ((virtual_extents, "virtual"), (physical_extents, "physical")):
        ordered = sorted(extents)
        if any(cur[0] < prev[1] for prev, cur in zip(ordered, ordered[1:])):
            raise RunningRamPayloadError(f"overlapping {label} PT_LOAD extents")
    if type(vector_address) is not int or vector_address & 3:
        raise RunningRamPayloadError("RAM vector address must be aligned")
    vector_segment = next(
        (
            s
            for s in segments
            if s["flags"] & _RAM_PAYLOAD_PF_X
            and s["vaddr"] <= vector_address
            and vector_address + 8 <= s["vaddr"] + s["filesz"]
        ),
        None,
    )
    if vector_segment is None:
        raise RunningRamPayloadError("RAM vector words are not file-backed executable bytes")
    v_off = vector_address - vector_segment["vaddr"]
    initial_msp, reset_pc = _struct.unpack_from("<II", vector_segment["data"], v_off)
    if not any(start < initial_msp <= limit for start, limit in _RAM_PAYLOAD_RANGES):
        raise RunningRamPayloadError("RAM ELF initial MSP is outside volatile SRAM")
    if not (reset_pc & 1 and (reset_pc & ~1) == (entry & ~1)):
        raise RunningRamPayloadError("RAM ELF reset vector does not match Thumb entry")
    if not any(
        s["flags"] & _RAM_PAYLOAD_PF_X and s["vaddr"] <= (reset_pc & ~1) < s["vaddr"] + s["filesz"]
        for s in segments
    ):
        raise RunningRamPayloadError("RAM ELF reset vector is outside executable SRAM")
    return segments, initial_msp, reset_pc


def _session_word(session, address: int) -> int:
    raw = bytes(session.read(address, 4))
    if len(raw) != 4:
        raise RunningRamPayloadError(f"short register read at 0x{address:08x}")
    return int.from_bytes(raw, "little")


def _payload_live_checks(session, segments, vector_address: int, *, running: bool):
    backend = session.backend
    dhcsr = _session_word(session, _RAM_PAYLOAD_DHCSR)
    if running and dhcsr & lifecycle.S_HALT:
        raise RunningRamPayloadError("campaign core is not running")
    if not running and not dhcsr & lifecycle.S_HALT:
        raise RunningRamPayloadError("campaign core did not halt")
    if _session_word(session, _RAM_PAYLOAD_VTOR) != vector_address:
        raise RunningRamPayloadError("live VTOR does not identify admitted RAM image")
    pc = int(backend.read_register("pc")) & 0xFFFFFFFF
    if not any(
        s["flags"] & _RAM_PAYLOAD_PF_X and s["vaddr"] <= (pc & ~1) < s["vaddr"] + s["filesz"]
        for s in segments
    ):
        raise RunningRamPayloadError("live PC is outside admitted executable SRAM")
    msp = int(backend.read_register("msp")) & 0xFFFFFFFF
    if not any(start < msp <= limit for start, limit in _RAM_PAYLOAD_RANGES):
        raise RunningRamPayloadError("live MSP is outside volatile SRAM")
    for segment in segments:
        if not segment["filesz"]:
            continue
        observed = bytes(session.read(segment["paddr"], segment["filesz"]))
        if (
            observed != segment["data"]
            or hashlib.sha256(observed).digest() != hashlib.sha256(segment["data"]).digest()
        ):
            raise RunningRamPayloadError(f"live PT_LOAD {segment['index']} readback differs")
    return pc


def restore_after_ram_payload(
    session,
    *,
    expected_device_uid: str,
    elf_path: str | _Path,
    expected_elf_sha256: str,
    vector_address: int,
    timeout: float = 10.0,
    poll: float = 0.05,
    clock=time.monotonic,
    sleep=time.sleep,
) -> dict:
    """Restore packaged RAM recovery firmware after proving a live RAM ELF.

    Requires a caller-owned GWProv DebugSession-like object with read, halt,
    reset(halt=True), resume, and watchdog-freeze operations. All source and
    recovery writes are constrained to volatile SRAM; flash is never written.
    GWProv has no campaign identity token yet, so this API binds to the supplied
    immutable UID and rechecks it before halt, after halt, and after reset.
    """
    required_session = ("read", "write", "halt", "reset", "resume", "enable_watchdog_freeze")
    if session is None or any(not callable(getattr(session, n, None)) for n in required_session):
        raise TypeError("GWProv session read/halt/reset/resume/watchdog APIs are required")
    backend = getattr(session, "backend", None)
    required_backend = (
        "read_register",
        "write_register",
        "reset_and_halt",
        "read_memory",
        "write_memory",
    )
    if backend is None or any(not callable(getattr(backend, n, None)) for n in required_backend):
        raise TypeError("GWProv backend register/reset APIs are required")
    if type(expected_device_uid) is not str or not expected_device_uid.startswith("stm32h7b0-uid:"):
        raise RunningRamPayloadError("expected STM32H7B0 UID is required")
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not isinstance(poll, (int, float))
        or isinstance(poll, bool)
        or timeout <= 0
        or poll <= 0
        or poll > timeout
    ):
        raise ValueError("recovery timeout bounds are invalid")
    try:
        raw = _Path(elf_path).read_bytes()
    except OSError as exc:
        raise RunningRamPayloadError("admitted RAM ELF cannot be read") from exc
    if hashlib.sha256(raw).hexdigest() != expected_elf_sha256:
        raise RunningRamPayloadError("RAM ELF SHA-256 differs from expected identity")
    segments, _msp, _pc = _parse_running_ram_elf(raw, vector_address)

    def uid():
        value = bytes(session.read(_RAM_PAYLOAD_UID, STM32H7B0_UID_BYTES))
        if len(value) != STM32H7B0_UID_BYTES or value in (bytes(12), b"\xff" * 12):
            raise RunningRamPayloadError("live device UID is unavailable")
        return "stm32h7b0-uid:" + value.hex()

    if uid() != expected_device_uid:
        raise RunningRamPayloadError("live device UID differs from expected identity")
    initial_pc = _payload_live_checks(session, segments, vector_address, running=True)
    session.halt()
    halted_pc = _payload_live_checks(session, segments, vector_address, running=False)
    if uid() != expected_device_uid:
        raise RunningRamPayloadError("live device UID changed while halting payload")
    frozen = session.enable_watchdog_freeze()
    if (
        type(frozen) is not dict
        or set(frozen) != {"WWDG1", "IWDG1"}
        or any(type(v) is not dict or v.get("enabled") is not True for v in frozen.values())
    ):
        raise RunningRamPayloadError("GWProv watchdog-freeze state is unverified")
    backend.reset_and_halt()
    if not _session_word(session, _RAM_PAYLOAD_DHCSR) & lifecycle.S_HALT:
        raise RunningRamPayloadError("GWProv reset did not leave core halted")
    if uid() != expected_device_uid:
        raise RunningRamPayloadError("live device UID changed across reset")
    image = _ram_flasher_image()
    _validate_image(image)
    chunks = _write_image_verified(backend, image)
    for address in (lifecycle.MAILBOX_STATUS, lifecycle.MAILBOX_STATUS + 4):
        session.write(address, bytes(4))
        if _session_word(session, address) != 0:
            raise RunningRamPayloadError("recovery mailbox clear readback differs")
    _set_vectors(backend, image)
    session.resume()
    deadline = clock() + timeout
    stable = 0
    last = {}
    while True:
        last = {
            "vtor": _session_word(session, lifecycle.VTOR),
            "dhcsr": _session_word(session, lifecycle.DHCSR),
            "status": _session_word(session, lifecycle.MAILBOX_STATUS),
        }
        if (
            last["vtor"] == lifecycle.GNWMANAGER_FIRMWARE_ADDR
            and not last["dhcsr"] & lifecycle.S_HALT
            and last["status"] == lifecycle.STATUS_IDLE
        ):
            stable += 1
            if stable >= 2:
                return {
                    **last,
                    "stubAlive": True,
                    "serviceState": "idle",
                    "watchdog_freeze": frozen,
                    "source_pc_before_halt": initial_pc,
                    "source_pc_halted": halted_pc,
                    "ram_image_start": lifecycle.GNWMANAGER_FIRMWARE_ADDR,
                    "ram_image_bytes": len(image),
                    "ram_image_chunks": chunks,
                    "ram_image_sha256": hashlib.sha256(image).hexdigest(),
                    "recovery_method": "GWProv-only-reset-and-RAM-stub",
                    "persistent_flash_write_performed": False,
                }
        else:
            stable = 0
        if clock() >= deadline:
            raise RunningRamPayloadError(
                "recovery stub failed stable IDLE verification: " + repr(last)
            )
        sleep(poll)
