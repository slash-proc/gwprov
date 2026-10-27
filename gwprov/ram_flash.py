"""GWProv-owned Bank1 programming through the resident RAM recovery stub."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from . import hw_lifecycle as lifecycle

BANK1_BASE = 0x08000000
BANK1_SIZE = 128 * 1024
BANK1_ERASE_SIZE = 8 * 1024
FLASHWORD_SIZE = 16
FLASH_IMAGE_CHUNK = 4096

COMM_BASE = 0x24025800
COMM_UPLOAD_IN_PROGRESS = COMM_BASE + 24
COMM_DOWNLOAD_IN_PROGRESS = COMM_BASE + 28
CONTEXT0 = COMM_BASE + 0x400
CONTEXT1 = COMM_BASE + 0x800
CONTEXT_BUFFER0 = COMM_BASE + 0x1000
CONTEXT_BUFFER_SIZE = 256 * 1024
CONTEXT_READY = 0x16C
CONTEXT_COUNTER = lifecycle.GNWMANAGER_CONTEXT_COUNTER
STATUS_IDLE = lifecycle.STATUS_IDLE
ACTION_ERASE_AND_FLASH = 0
UID_BASE = 0x08FFF800
UID_SIZE = 12
STACK_RAM_START = 0x20000000
STACK_RAM_END = 0x20020000


class _LifecycleReader:
    """Adapt a GWProv debug session to the lifecycle word-read interface."""

    def __init__(self, session):
        self.session = session

    def read_uint32(self, address: int) -> int:
        return self.session.u32(address)


def _write_verified(session, address: int, payload: bytes) -> None:
    for offset in range(0, len(payload), FLASH_IMAGE_CHUNK):
        chunk = payload[offset : offset + FLASH_IMAGE_CHUNK]
        session.write(address + offset, chunk)
        if session.read(address + offset, len(chunk)) != chunk:
            raise RuntimeError(f"RAM image transfer readback differs at 0x{address + offset:08x}")


def program_bank1_with_recovery_stub(
    session, image_path: str | Path, *, timeout: float = 120
) -> dict:
    """Program Bank1 using the already-running RAM recovery stub's mailbox.

    This mirrors its documented ERASE_AND_FLASH mailbox ABI directly through
    GWProv session memory operations; it creates no GnW client/process and
    never resets or writes Bank2/external flash. The stub must already be live
    and idle. Bank1 bounds and 8 KiB/16-byte flash geometry follow the tracked
    STM32H7B0 device headers and the project's internal-flash diagnostic.
    """
    if getattr(session, "transport", None) != "remote-gdb":
        raise RuntimeError("Bank1 RAM-stub programming requires a remote-GDB GWProv session")
    if getattr(session, "target_profile", None) != "stm32h7b0":
        raise RuntimeError("Bank1 RAM-stub programming requires target profile 'stm32h7b0'")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ValueError("timeout must be positive")

    image = Path(image_path).expanduser().read_bytes()
    if len(image) < 8 or len(image) > BANK1_SIZE:
        raise ValueError(f"Bank1 diagnostic image must be 8..{BANK1_SIZE} bytes")
    initial_sp = int.from_bytes(image[:4], "little")
    initial_pc = int.from_bytes(image[4:8], "little")
    if not (STACK_RAM_START < initial_sp <= STACK_RAM_END and initial_sp % 8 == 0):
        raise ValueError("Bank1 image initial stack pointer is outside aligned STM32H7B0 SRAM")
    if not (initial_pc & 1 and BANK1_BASE <= (initial_pc & ~1) < BANK1_BASE + len(image)):
        raise ValueError("Bank1 image reset vector does not point into its Thumb image")

    payload_size = (len(image) + BANK1_ERASE_SIZE - 1) & ~(BANK1_ERASE_SIZE - 1)
    if payload_size > BANK1_SIZE or payload_size > CONTEXT_BUFFER_SIZE:
        raise ValueError("Bank1 image plus required erase padding exceeds supported capacity")
    payload = image.ljust(payload_size, b"\xff")
    digest = hashlib.sha256(payload).digest()

    state = lifecycle.inspect_target_mode(_LifecycleReader(session))
    if not (
        state["stubResident"]
        and state["stubAlive"]
        and state["serviceState"] == "idle"
        and state["halted"] is False
    ):
        raise RuntimeError("Bank1 write requires an already-running idle RAM recovery stub")
    if state.get("locked") is not False:
        raise RuntimeError("Bank1 write requires verified unlocked STM32H7B0 flash")

    uid_before = bytes(session.read(UID_BASE, UID_SIZE))
    if len(uid_before) != UID_SIZE or uid_before in (bytes(UID_SIZE), b"\xff" * UID_SIZE):
        raise RuntimeError("STM32H7B0 UID is unavailable; refusing Bank1 programming")

    # The RAM stub accepts a command only when `ready == context_counter`.
    # This keeps the operation serialized with its existing work queue.
    if session.u32(COMM_UPLOAD_IN_PROGRESS) or session.u32(COMM_DOWNLOAD_IN_PROGRESS):
        raise RuntimeError("RAM recovery stub has an in-flight transfer")
    if session.u32(CONTEXT0 + CONTEXT_READY) or session.u32(CONTEXT1 + CONTEXT_READY):
        raise RuntimeError("RAM recovery stub has an occupied work context")
    if session.u32(COMM_BASE) != STATUS_IDLE:
        raise RuntimeError("RAM recovery stub left IDLE during Bank1 preflight")
    counter = session.u32(CONTEXT_COUNTER)
    if not 1 <= counter <= 0xFFFFFFFE:
        raise RuntimeError(f"RAM recovery context counter is invalid: {counter}")

    # Fill a free command slot while keeping ready clear. Drain posted AP writes
    # by reading the buffer, then set ready last exactly as the host ABI requires.
    session.write(CONTEXT0, bytes(1024))
    session.write(CONTEXT_BUFFER0, payload)
    if session.read(CONTEXT_BUFFER0, len(payload)) != payload:
        raise RuntimeError("Bank1 source buffer RAM readback differs; command not armed")
    session.write_u32(CONTEXT0 + 4, len(payload))
    session.write_u32(CONTEXT0 + 8, 0)
    session.write_u32(CONTEXT0 + 12, 1)
    session.write_u32(CONTEXT0 + 16, len(payload))
    session.write_u32(CONTEXT0 + 20, 0)
    session.write(CONTEXT0 + 24, digest)
    session.write_u32(CONTEXT0 + 56, 1)
    session.write_u32(CONTEXT0 + 60, ACTION_ERASE_AND_FLASH)
    session.write_u32(CONTEXT0 + 64, 0)
    session.u32(CONTEXT_BUFFER0)
    if bytes(session.read(UID_BASE, UID_SIZE)) != uid_before:
        raise RuntimeError("device UID changed during RAM staging; command not armed")
    if session.u32(COMM_BASE) != STATUS_IDLE:
        raise RuntimeError("RAM recovery stub is no longer idle; command not armed")
    session.write_u32(COMM_UPLOAD_IN_PROGRESS, 1)
    try:
        session.write_u32(CONTEXT0 + CONTEXT_READY, counter)
    finally:
        session.write_u32(COMM_UPLOAD_IN_PROGRESS, 0)

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        status = session.u32(COMM_BASE)
        ready = session.u32(CONTEXT0 + CONTEXT_READY)
        uploading = session.u32(COMM_UPLOAD_IN_PROGRESS)
        last = {"status": status, "ready": ready, "upload_in_progress": uploading}
        if status & 0xFFFF0000 == 0xBAD00000:
            raise RuntimeError(f"RAM recovery stub reported flash failure 0x{status:08x}")
        if status == STATUS_IDLE and ready == 0 and uploading == 0:
            break
        time.sleep(0.05)
    else:
        raise TimeoutError(f"Bank1 flash command did not complete; last mailbox state: {last}")

    uid_after = bytes(session.read(UID_BASE, UID_SIZE))
    if uid_after != uid_before:
        raise RuntimeError("device UID changed during Bank1 programming")
    actual = bytes(session.read(BANK1_BASE, len(payload)))
    actual_hash = hashlib.sha256(actual).digest()
    if actual != payload or actual_hash != digest:
        raise RuntimeError(
            "Bank1 flash readback differs: "
            f"expected sha256={digest.hex()}, actual sha256={actual_hash.hex()}"
        )
    return {
        "bank": 1,
        "address": BANK1_BASE,
        "image_bytes": len(image),
        "programmed_bytes": len(payload),
        "sha256": actual_hash.hex(),
        "verified": True,
        "device_uid": uid_before.hex(),
        "recovery_state": "idle",
    }
