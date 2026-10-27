import struct

import pytest

from gwprov import hw_lifecycle, ram_recovery


class FakeBackend:
    def __init__(self):
        self.running = True
        self.memory = bytearray(0x10000)
        self.regs = {"msp": 0x20020000, "pc": 0x240E6800, "xpsr": 1 << 24}
        self.register_numbers = {"xpsr": 16}
        self.uid = bytes.fromhex("2d0049001351303137363837")
        self.words = {
            hw_lifecycle.VTOR: 0x240E6800,
            hw_lifecycle.DHCSR: 0,
            hw_lifecycle.MAILBOX_STATUS: hw_lifecycle.STATUS_IDLE,
            hw_lifecycle.FLASH_OPTSR_CUR: 0xFFFFFFFF,
            hw_lifecycle.GNWMANAGER_CONTEXT_COUNTER: 7,
            0x5C001034: 0,
            0x5C001054: 0,
        }
        self.writes = []
        self.corrupt_readbacks = 0
        self.reset_count = 0

    def read_uint32(self, address):
        if address == hw_lifecycle.DHCSR:
            return hw_lifecycle.S_HALT if not self.running else 0
        return self.words.get(address, 0)

    def write_uint32(self, address, value):
        self.writes.append(("word", address, value))
        self.words[address] = value

    def read_memory(self, address, size):
        if address == ram_recovery.STM32H7B0_UID_BASE:
            return self.uid[:size]
        offset = address - hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR
        if 0 <= offset <= len(self.memory) - size:
            result = bytes(self.memory[offset : offset + size])
            if self.corrupt_readbacks:
                self.corrupt_readbacks -= 1
                return bytes([result[0] ^ 1]) + result[1:]
            return result
        return bytes(size)

    def write_memory(self, address, data):
        self.writes.append(("memory", address, bytes(data)))
        offset = address - hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR
        assert 0 <= offset <= len(self.memory) - len(data)
        assert len(data) <= ram_recovery.RAM_CHUNK_BYTES
        self.memory[offset : offset + len(data)] = data

    def reset_and_halt(self):
        self.reset_count += 1
        self.running = False
        self.words[hw_lifecycle.VTOR] = 0x08000000
        self.words[hw_lifecycle.MAILBOX_STATUS] = 0

    def halt(self):
        self.running = False

    def write_register(self, name, value):
        self.writes.append(("register", name, value))
        self.regs[name] = value & ~1 if name == "pc" else value

    def read_register(self, name):
        return self.regs[name]

    def resume(self):
        self.running = True
        self.words[hw_lifecycle.VTOR] = 0x240E6800
        self.words[hw_lifecycle.MAILBOX_STATUS] = hw_lifecycle.STATUS_IDLE


def image(msp=0x20020000):
    return struct.pack("<II", msp, 0x240E6801) + bytes(i % 251 for i in range(5000))


def test_ram_only_restart_is_chunked_verifies_idle_and_restores_watchdogs(monkeypatch):
    raw = image()
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: raw)
    backend = FakeBackend()
    result = ram_recovery.start_recovery_stub_ram_only(backend)
    assert result["stubAlive"] is True
    assert result["started"] is True
    assert result["device_uid"] == "stm32h7b0-uid:" + backend.uid.hex()
    assert result["persistent_flash_write_performed"] is False
    assert result["ram_image_bytes"] == len(raw)
    assert result["ram_image_chunks"] == 2
    assert result["watchdog_freezes_restored"] is True
    assert result["watchdog_state_after"] == result["watchdog_state_before"]
    assert result["reset_observed_at_ns"] > 0
    assert all(
        not (kind == "memory" and 0x08000000 <= address < 0x08200000)
        for kind, address, *_ in backend.writes
    )


def test_bad_stack_vector_fails_before_reset_or_target_writes(monkeypatch):
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image(0x2001FFF4))
    backend = FakeBackend()
    with pytest.raises(hw_lifecycle.TargetModeError, match="aligned within SRAM"):
        ram_recovery.start_recovery_stub_ram_only(backend)
    assert backend.reset_count == 0
    assert backend.writes == []


def test_non_idle_target_is_rejected_before_reset(monkeypatch):
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image())
    backend = FakeBackend()
    backend.words[hw_lifecycle.MAILBOX_STATUS] = 0xCAFE0002
    with pytest.raises(hw_lifecycle.TargetModeError, match="already-running idle"):
        ram_recovery.start_recovery_stub_ram_only(backend)
    assert backend.reset_count == 0
    assert backend.writes == []


def test_transfer_failure_attempts_reload_and_reports_verified_idle(monkeypatch):
    raw = image()
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: raw)
    backend = FakeBackend()
    backend.corrupt_readbacks = 1
    with pytest.raises(ram_recovery.RamRecoveryError) as caught:
        ram_recovery.start_recovery_stub_ram_only(backend)
    assert caught.value.receipt["recovery_state"] == "live_idle_after_reload"
    assert caught.value.receipt["state"]["stubAlive"] is True
    assert caught.value.receipt["watchdog_freezes_restored"] is True


def test_unrecoverable_transfer_returns_precise_halted_receipt(monkeypatch):
    raw = image()
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: raw)
    backend = FakeBackend()
    backend.corrupt_readbacks = 100
    with pytest.raises(ram_recovery.RamRecoveryError) as caught:
        ram_recovery.start_recovery_stub_ram_only(backend)
    receipt = caught.value.receipt
    assert receipt["recovery_state"] == "core_halted_fail_closed"
    assert receipt["core_halted"] is True
    assert receipt["watchdog_freezes_restored"] is False
    assert receipt["watchdog_freezes_left_for_halted_core"]


def test_halted_stub_repair_checks_identity_image_and_resumes_without_reset(monkeypatch):
    raw = image()
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: raw)
    backend = FakeBackend()
    backend.running = False
    backend.words[hw_lifecycle.VTOR] = ram_recovery.RESET_FLASH_VTOR
    backend.words[hw_lifecycle.MAILBOX_STATUS] = 0
    backend.memory[: len(raw)] = raw
    result = ram_recovery.resume_halted_ram_stub(
        backend, expected_uid="stm32h7b0-uid:" + backend.uid.hex()
    )
    assert result["resumed"] is True
    assert result["stubAlive"] is True
    assert backend.reset_count == 0
    assert [row[0] for row in backend.writes] == ["register", "register"]
    assert backend.words[0x5C001034] == 0
    assert backend.words[0x5C001054] == 0


def test_halted_stub_repair_refuses_wrong_identity_without_writes(monkeypatch):
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image())
    backend = FakeBackend()
    backend.running = False
    backend.words[hw_lifecycle.VTOR] = ram_recovery.RESET_FLASH_VTOR
    backend.words[hw_lifecycle.MAILBOX_STATUS] = 0
    with pytest.raises(hw_lifecycle.TargetModeError, match="UID differs"):
        ram_recovery.resume_halted_ram_stub(backend, expected_uid="stm32h7b0-uid:wrong")
    assert backend.writes == []
    assert backend.reset_count == 0


class RamPayloadBackend:
    def __init__(self, payload_address, payload):
        self.uid = bytes.fromhex("2d0049001351303137363837")
        self.running = True
        self.regs = {"msp": 0x20020000, "pc": payload_address + 8}
        self.words = {
            hw_lifecycle.VTOR: payload_address,
            hw_lifecycle.MAILBOX_STATUS: 0x12345678,
            hw_lifecycle.MAILBOX_STATUS + 4: 0x87654321,
            hw_lifecycle.DHCSR: 0,
            hw_lifecycle.GNWMANAGER_CONTEXT_COUNTER: 1,
            ram_recovery.RESET_FLASH_VTOR: 0,
        }
        self.regions = {payload_address: bytearray(payload)}
        self.writes = []
        self.corrupt_payload = False

    def read_register(self, name):
        return self.regs[name]

    def write_register(self, name, value):
        self.writes.append(("register", name, value))
        self.regs[name] = value & ~1 if name == "pc" else value

    def _find(self, address, size):
        for start, region in self.regions.items():
            if start <= address and address + size <= start + len(region):
                return region, address - start
        return None, None

    def read_memory(self, address, size):
        if address == ram_recovery.STM32H7B0_UID_BASE:
            return self.uid[:size]
        if address in self.words and size == 4:
            value = self.words[address]
            if address == hw_lifecycle.DHCSR:
                value = hw_lifecycle.S_HALT if not self.running else 0
            return value.to_bytes(4, "little")
        region, offset = self._find(address, size)
        if region is None:
            return bytes(size)
        result = bytes(region[offset : offset + size])
        if self.corrupt_payload and address != hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR:
            return bytes([result[0] ^ 1]) + result[1:]
        return result

    def write_memory(self, address, data):
        self.writes.append(("memory", address, bytes(data)))
        if address in self.words and len(data) == 4:
            self.words[address] = int.from_bytes(data, "little")
            return
        region, offset = self._find(address, len(data))
        if region is None:
            region = bytearray(len(data))
            self.regions[address] = region
            offset = 0
        region[offset : offset + len(data)] = data

    def read_uint32(self, address):
        return int.from_bytes(self.read_memory(address, 4), "little")

    def write_uint32(self, address, value):
        self.writes.append(("word", address, value))
        self.words[address] = value

    def halt(self):
        self.running = False

    def reset_and_halt(self):
        self.running = False
        self.words[hw_lifecycle.VTOR] = ram_recovery.RESET_FLASH_VTOR
        self.words[hw_lifecycle.MAILBOX_STATUS] = 0

    def resume(self):
        self.running = True
        self.words[hw_lifecycle.VTOR] = hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR
        self.words[hw_lifecycle.MAILBOX_STATUS] = hw_lifecycle.STATUS_IDLE


class RamPayloadSession:
    transport = "fake"

    def __init__(self, backend):
        self.backend = backend

    def read(self, address, size=4):
        return self.backend.read_memory(address, size)

    def write(self, address, data):
        self.backend.write_memory(address, data)

    def halt(self):
        self.backend.halt()

    def reset(self, halt=False):
        if halt:
            self.backend.reset_and_halt()
        else:
            raise AssertionError("unexpected non-halted reset")

    def resume(self):
        self.backend.resume()

    def enable_watchdog_freeze(self):
        return {
            "WWDG1": {"enabled": True, "already_enabled": True},
            "IWDG1": {"enabled": True, "already_enabled": True},
        }


def ram_payload_elf(*, paddr=0x24001000):
    import hashlib

    ident = b"\x7fELF\x01\x01\x01" + bytes(9)
    data = struct.pack("<II", 0x20020000, 0x24001009) + bytes(range(12))
    header = struct.pack(
        "<16sHHIIIIIHHHHHH", ident, 2, 40, 1, 0x24001008, 52, 0, 0, 52, 32, 1, 0, 0, 0
    )
    phdr = struct.pack("<8I", 1, 84, 0x24001000, paddr, len(data), len(data), 5, 4)
    raw = header + phdr + data
    return raw, hashlib.sha256(raw).hexdigest()


def test_restore_after_running_ram_payload_is_gwprov_only(monkeypatch, tmp_path):
    raw, digest = ram_payload_elf()
    elf = tmp_path / "campaign.elf"
    elf.write_bytes(raw)
    backend = RamPayloadBackend(0x24001000, raw[84:])
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image())
    result = ram_recovery.restore_after_ram_payload(
        RamPayloadSession(backend),
        expected_device_uid="stm32h7b0-uid:" + backend.uid.hex(),
        elf_path=elf,
        expected_elf_sha256=digest,
        vector_address=0x24001000,
        clock=iter([0.0, 0.0, 0.0]).__next__,
        sleep=lambda _delay: None,
    )
    assert result["stubAlive"] is True
    assert result["serviceState"] == "idle"
    assert result["persistent_flash_write_performed"] is False
    assert result["watchdog_freeze"]["WWDG1"]["enabled"] is True
    assert backend.running
    assert all(
        not (kind == "memory" and 0x08000000 <= address < 0x08200000)
        for kind, address, *_ in backend.writes
    )


def test_restore_rejects_flash_pt_load_before_any_target_mutation(monkeypatch, tmp_path):
    raw, digest = ram_payload_elf(paddr=0x08001000)
    elf = tmp_path / "flash-segment.elf"
    elf.write_bytes(raw)
    backend = RamPayloadBackend(0x24001000, raw[84:])
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image())
    with pytest.raises(
        ram_recovery.RunningRamPayloadError, match="physical extent is outside SRAM"
    ):
        ram_recovery.restore_after_ram_payload(
            RamPayloadSession(backend),
            expected_device_uid="stm32h7b0-uid:" + backend.uid.hex(),
            elf_path=elf,
            expected_elf_sha256=digest,
            vector_address=0x24001000,
        )
    assert backend.writes == []


def test_restore_rejects_running_payload_readback_mismatch_before_halt(monkeypatch, tmp_path):
    raw, digest = ram_payload_elf()
    elf = tmp_path / "campaign.elf"
    elf.write_bytes(raw)
    backend = RamPayloadBackend(0x24001000, raw[84:])
    backend.corrupt_payload = True
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: image())
    with pytest.raises(ram_recovery.RunningRamPayloadError, match="readback differs"):
        ram_recovery.restore_after_ram_payload(
            RamPayloadSession(backend),
            expected_device_uid="stm32h7b0-uid:" + backend.uid.hex(),
            elf_path=elf,
            expected_elf_sha256=digest,
            vector_address=0x24001000,
        )
    assert backend.running
    assert backend.writes == []


def test_restore_requires_expected_live_uid_before_mutation(tmp_path):
    raw, digest = ram_payload_elf()
    elf = tmp_path / "campaign.elf"
    elf.write_bytes(raw)
    backend = RamPayloadBackend(0x24001000, raw[84:])
    with pytest.raises(ram_recovery.RunningRamPayloadError, match="UID differs"):
        ram_recovery.restore_after_ram_payload(
            RamPayloadSession(backend),
            expected_device_uid="stm32h7b0-uid:wrong",
            elf_path=elf,
            expected_elf_sha256=digest,
            vector_address=0x24001000,
        )
    assert backend.writes == []
