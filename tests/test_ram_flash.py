import hashlib

import pytest

from gwprov.hw_lifecycle import (
    DBGMCU_APB3FZ1,
    DBGMCU_APB4FZ1,
    DHCSR,
    GNWMANAGER_CONTEXT_COUNTER,
    MAILBOX_STATUS,
    STATUS_IDLE,
    VTOR,
)
from gwprov.ram_flash import (
    BANK1_BASE,
    BANK1_ERASE_SIZE,
    BANK1_SIZE,
    COMM_BASE,
    COMM_DOWNLOAD_IN_PROGRESS,
    COMM_UPLOAD_IN_PROGRESS,
    CONTEXT0,
    CONTEXT1,
    CONTEXT_BUFFER0,
    CONTEXT_READY,
    UID_BASE,
    program_bank1_with_recovery_stub,
)


class FakeRecoverySession:
    transport = "remote-gdb"
    target_profile = "stm32h7b0"

    def __init__(self, *, vtor=0x240E6800, status=STATUS_IDLE, locked=False):
        self.memory = {}
        self.writes = []
        self.status_reads = []
        self.flash = bytearray(b"\x00" * BANK1_SIZE)
        self.memory[UID_BASE] = b"\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c"
        option = 0x0000AA00 if not locked else 0x00000100
        for address, value in {
            VTOR: vtor,
            DHCSR: 0,
            MAILBOX_STATUS: status,
            DBGMCU_APB3FZ1: 0,
            DBGMCU_APB4FZ1: 0,
            COMM_BASE: status,
            COMM_UPLOAD_IN_PROGRESS: 0,
            COMM_DOWNLOAD_IN_PROGRESS: 0,
            CONTEXT0 + CONTEXT_READY: 0,
            CONTEXT1 + CONTEXT_READY: 0,
            GNWMANAGER_CONTEXT_COUNTER: 7,
            0x5200201C: option,
        }.items():
            self.memory[address] = value.to_bytes(4, "little")

    def read(self, address, size=4):
        if BANK1_BASE <= address and address + size <= BANK1_BASE + BANK1_SIZE:
            start = address - BANK1_BASE
            return bytes(self.flash[start : start + size])
        for base, data in sorted(self.memory.items(), reverse=True):
            if base <= address and address + size <= base + len(data):
                offset = address - base
                return data[offset : offset + size]
        return bytes(size)

    def u32(self, address):
        if address == COMM_BASE and self.status_reads:
            return self.status_reads.pop(0)
        return int.from_bytes(self.read(address, 4), "little")

    def write(self, address, data):
        if BANK1_BASE <= address < BANK1_BASE + BANK1_SIZE:
            raise AssertionError("GWProv host must not write memory-mapped flash directly")
        self.writes.append((address, bytes(data)))
        self.memory[address] = bytes(data)
        if address == CONTEXT0 + CONTEXT_READY and int.from_bytes(data, "little"):
            self._execute_flash_command()

    def write_u32(self, address, value):
        self.write(address, value.to_bytes(4, "little"))

    def _execute_flash_command(self):
        assert self.u32(COMM_UPLOAD_IN_PROGRESS) == 1
        assert self.u32(CONTEXT0 + 8) == 0
        assert self.u32(CONTEXT0 + 12) == 1
        assert self.u32(CONTEXT0 + 56) == 1
        assert self.u32(CONTEXT0 + 60) == 0
        size = self.u32(CONTEXT0 + 4)
        erase_size = self.u32(CONTEXT0 + 16)
        payload = self.read(CONTEXT_BUFFER0, size)
        expected_hash = self.read(CONTEXT0 + 24, 32)
        assert size == erase_size
        assert size % BANK1_ERASE_SIZE == 0
        assert hashlib.sha256(payload).digest() == expected_hash
        self.flash[:erase_size] = b"\xff" * erase_size
        self.flash[:size] = payload
        self.memory[CONTEXT0 + CONTEXT_READY] = bytes(4)
        self.status_reads.extend([0xCAFE0001, 0xCAFE0002, 0xCAFE0003, STATUS_IDLE])


def _image(size=96):
    image = bytearray(b"\x00" * size)
    image[:4] = (0x20020000).to_bytes(4, "little")
    image[4:8] = (BANK1_BASE + 0x21).to_bytes(4, "little")
    return bytes(image)


def test_program_bank1_uses_idle_ram_stub_mailbox_and_readback(tmp_path):
    image_path = tmp_path / "diag.bin"
    image_path.write_bytes(_image())
    session = FakeRecoverySession()

    result = program_bank1_with_recovery_stub(session, image_path)

    programmed = _image().ljust(BANK1_ERASE_SIZE, b"\xff")
    assert result == {
        "bank": 1,
        "address": BANK1_BASE,
        "image_bytes": len(_image()),
        "programmed_bytes": len(programmed),
        "sha256": hashlib.sha256(programmed).hexdigest(),
        "verified": True,
        "device_uid": session.read(UID_BASE, 12).hex(),
        "recovery_state": "idle",
    }
    assert session.flash[: len(programmed)] == programmed
    assert session.u32(COMM_UPLOAD_IN_PROGRESS) == 0
    assert all(
        not (BANK1_BASE <= address < BANK1_BASE + BANK1_SIZE) for address, _ in session.writes
    )
    assert session.u32(CONTEXT0 + 56) == 1
    assert session.u32(CONTEXT0 + 8) == 0


@pytest.mark.parametrize(
    "session,match",
    [
        (FakeRecoverySession(status=0xCAFE0001), "already-running idle RAM recovery stub"),
        (FakeRecoverySession(locked=True), "verified unlocked STM32H7B0 flash"),
        (FakeRecoverySession(vtor=0x08000000), "already-running idle RAM recovery stub"),
    ],
)
def test_program_bank1_fails_before_any_ram_write_when_stub_preflight_fails(
    tmp_path, session, match
):
    image_path = tmp_path / "diag.bin"
    image_path.write_bytes(_image())

    with pytest.raises(RuntimeError, match=match):
        program_bank1_with_recovery_stub(session, image_path)

    assert session.writes == []


def test_program_bank1_enforces_h7b0_capacity_and_reset_vector(tmp_path):
    image_path = tmp_path / "bad.bin"
    image_path.write_bytes(_image(BANK1_SIZE + 1))
    with pytest.raises(ValueError, match="Bank1 diagnostic image"):
        program_bank1_with_recovery_stub(FakeRecoverySession(), image_path)

    image_path.write_bytes(bytes(32))
    with pytest.raises(ValueError, match="initial stack pointer"):
        program_bank1_with_recovery_stub(FakeRecoverySession(), image_path)


def test_program_bank1_rejects_non_remote_transport(tmp_path):
    image_path = tmp_path / "diag.bin"
    image_path.write_bytes(_image())
    session = FakeRecoverySession()
    session.transport = "hardware"
    with pytest.raises(RuntimeError, match="remote-GDB GWProv session"):
        program_bank1_with_recovery_stub(session, image_path)


def test_debug_console_api_delegates_to_ram_stub_programmer(tmp_path, monkeypatch):
    from gwprov.debug_shell import DebugSession, RemoteGDBBackend
    from gwprov import ram_flash

    backend = RemoteGDBBackend(
        "debug-target.example.invalid", 3333, daemon_managed=True, target_profile="stm32h7b0"
    )
    session = DebugSession(backend, "remote-gdb")
    expected = {"bank": 1, "verified": True}
    calls = []
    monkeypatch.setattr(
        ram_flash,
        "program_bank1_with_recovery_stub",
        lambda actual_session, image_path, *, timeout: (
            calls.append((actual_session, image_path, timeout)) or expected
        ),
    )

    assert session.flash_bank1(tmp_path / "diagnostic.bin", timeout=17) == expected
    assert calls == [(session, tmp_path / "diagnostic.bin", 17)]


def test_debug_console_api_rejects_non_remote_backend():
    from gwprov.debug_shell import DebugSession

    with pytest.raises(RuntimeError, match="direct remote-GDB session"):
        DebugSession(object(), "remote-gdb").flash_bank1("ignored.bin")
