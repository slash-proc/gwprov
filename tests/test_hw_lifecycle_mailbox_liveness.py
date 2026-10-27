import pytest

from gwprov import hw_lifecycle


class Backend:
    def __init__(
        self,
        *,
        vtor=hw_lifecycle.VTOR_BANK1_START,
        pc=hw_lifecycle.VTOR_BANK1_START + 0x101,
        status=hw_lifecycle.STATUS_IDLE,
    ):
        self.running = True
        self.register_pc = pc
        self.words = {
            hw_lifecycle.VTOR: vtor,
            hw_lifecycle.MAILBOX_STATUS: status,
            hw_lifecycle.FLASH_OPTSR_CUR: 0xFFFFFFFF,
        }
        self.halts = 0
        self.resumes = 0

    def read_uint32(self, address):
        if address == hw_lifecycle.DHCSR:
            return 0 if self.running else hw_lifecycle.S_HALT
        return self.words.get(address, 0)

    def halt(self):
        self.halts += 1
        self.running = False

    def resume(self):
        self.resumes += 1
        self.running = True

    def read_register(self, name):
        if name == "pc":
            return self.register_pc
        raise KeyError(name)


def test_passive_flash_vtor_idle_mailbox_is_only_candidate(monkeypatch):
    backend = Backend()
    monkeypatch.setattr(
        __import__("gwprov.ram_recovery", fromlist=["_ram_flasher_image"]),
        "_ram_flasher_image",
        lambda: b"x" * 0x1000,
    )
    state = hw_lifecycle.inspect_target_mode(backend)
    assert state["mailboxIdleCandidate"] is True
    assert state["stubAlive"] is False
    assert state["stubDetected"] is False
    assert state["mailboxPcVerified"] is None
    assert backend.halts == backend.resumes == 0


def test_stale_flash_vtor_idle_is_rejected_by_pc_and_restores_running_state(monkeypatch):
    backend = Backend(pc=hw_lifecycle.VTOR_BANK1_START + 0x201)
    monkeypatch.setattr(
        __import__("gwprov.ram_recovery", fromlist=["_ram_flasher_image"]),
        "_ram_flasher_image",
        lambda: b"x" * 0x1000,
    )
    state = hw_lifecycle.inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    assert state["mailboxPcVerified"] is False
    assert state["stubAlive"] is False
    assert state["stubDetected"] is False
    assert backend.running is True
    assert backend.halts == backend.resumes == 1


def test_live_flash_vtor_service_is_kept_when_pc_is_in_packaged_image(monkeypatch):
    backend = Backend(pc=hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR + 0x121)
    monkeypatch.setattr(
        __import__("gwprov.ram_recovery", fromlist=["_ram_flasher_image"]),
        "_ram_flasher_image",
        lambda: b"x" * 0x1000,
    )
    state = hw_lifecycle.inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    assert state["mailboxPcVerified"] is True
    assert state["stubAlive"] is True
    assert state["stubDetected"] is True
    assert backend.running is True
    assert backend.halts == backend.resumes == 1


def test_unreadable_pc_fails_closed(monkeypatch):
    backend = Backend()
    backend.read_register = lambda _name: (_ for _ in ()).throw(RuntimeError("no PC"))
    monkeypatch.setattr(
        __import__("gwprov.ram_recovery", fromlist=["_ram_flasher_image"]),
        "_ram_flasher_image",
        lambda: b"x" * 0x1000,
    )
    state = hw_lifecycle.inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    assert state["mailboxPcVerified"] is None
    assert state["stubAlive"] is False
    assert state["stubDetected"] is True
    assert backend.running is True
    assert backend.halts == backend.resumes == 1


def test_recover_starts_stub_for_stale_flash_vtor_mailbox(monkeypatch):
    from gwprov import ram_recovery

    backend = Backend(pc=hw_lifecycle.VTOR_BANK1_START + 0x201)
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: b"x" * 0x1000)
    starts = []
    attaches = []

    def start(_backend, _gnw):
        starts.append(True)
        backend.register_pc = hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR + 0x121
        backend.words[hw_lifecycle.VTOR] = hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR

    monkeypatch.setattr(hw_lifecycle, "start_gnwmanager_verified", start)
    monkeypatch.setattr(
        hw_lifecycle, "attach_to_idle_gnwmanager", lambda *_a, **_k: attaches.append(True) or 7
    )
    result = hw_lifecycle.recover_flasher(backend, gnw=object())
    assert starts == [True]
    assert len(attaches) == 1
    assert result["started"] is True
    assert result["stubAlive"] is True


def test_recover_attaches_to_verified_flash_vtor_service(monkeypatch):
    from gwprov import ram_recovery

    backend = Backend(pc=hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR + 0x121)
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: b"x" * 0x1000)
    starts = []
    attaches = []
    monkeypatch.setattr(hw_lifecycle, "start_gnwmanager_verified", lambda *_a: starts.append(True))
    monkeypatch.setattr(
        hw_lifecycle, "attach_to_idle_gnwmanager", lambda *_a, **_k: attaches.append(True) or 9
    )
    result = hw_lifecycle.recover_flasher(backend, gnw=object())
    assert starts == []
    assert attaches == [True]
    assert result["started"] is False
    assert result["stubAlive"] is True


@pytest.mark.parametrize("status", sorted(hw_lifecycle.GNWMANAGER_ACTIVE_STATUSES))
def test_stale_busy_flash_mailbox_recovers_without_manual_ram_edit(monkeypatch, status):
    from gwprov import ram_recovery

    backend = Backend(
        vtor=hw_lifecycle.BOOTLOADER_START, pc=hw_lifecycle.BOOTLOADER_START + 0x2FF, status=status
    )
    backend.words[hw_lifecycle.FLASH_OPTSR_CUR] = 0xAA00
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: b"x" * 0x1000)
    state = hw_lifecycle.inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    assert state["staleBusyMailbox"] is True
    assert state["stubDetected"] is False
    assert backend.running and backend.halts == backend.resumes == 2
    starts = []

    def start(*_args):
        starts.append(True)
        backend.words[hw_lifecycle.VTOR] = hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR
        backend.words[hw_lifecycle.MAILBOX_STATUS] = hw_lifecycle.STATUS_IDLE

    monkeypatch.setattr(hw_lifecycle, "start_gnwmanager_verified", start)
    monkeypatch.setattr(hw_lifecycle, "attach_to_idle_gnwmanager", lambda *_a: 1)
    assert hw_lifecycle.recover_flasher(backend, gnw=object())["stubAlive"]
    assert starts == [True]


@pytest.mark.parametrize(
    "conflict",
    [
        "flash_busy",
        "unknown_controller",
        "locked",
        "unknown_lock",
        "ram_pc",
        "live_stub",
        "unreadable_pc",
        "changed_vtor",
        "changed_mailbox",
        "changed_pc",
    ],
)
def test_busy_mailbox_recovery_preserves_unproven_or_live_service(monkeypatch, conflict):
    from gwprov import ram_recovery

    backend = Backend(
        vtor=hw_lifecycle.BOOTLOADER_START,
        pc=hw_lifecycle.BOOTLOADER_START + 0x2FF,
        status=0xCAFE0001,
    )
    backend.words[hw_lifecycle.FLASH_OPTSR_CUR] = 0xAA00
    if conflict == "flash_busy":
        backend.words[0x52002110] = 1
    if conflict == "unknown_controller":
        backend.words[0x52002010] = 0xFFFFFFFF
    if conflict == "locked":
        backend.words[hw_lifecycle.FLASH_OPTSR_CUR] = 0xBB00
    if conflict == "unknown_lock":
        backend.words[hw_lifecycle.FLASH_OPTSR_CUR] = 0xFFFFFFFF
    if conflict == "ram_pc":
        backend.register_pc = 0x24050001
    if conflict == "live_stub":
        backend.register_pc = hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR + 0x121
    if conflict == "unreadable_pc":
        backend.read_register = lambda _n: (_ for _ in ()).throw(RuntimeError("no PC"))
    original = backend.read_register
    count = 0

    def register(name):
        nonlocal count
        count += 1
        if count == 2:
            if conflict == "changed_vtor":
                backend.words[hw_lifecycle.VTOR] += 0x1000
            if conflict == "changed_mailbox":
                backend.words[hw_lifecycle.MAILBOX_STATUS] += 1
            if conflict == "changed_pc":
                return hw_lifecycle.GNWMANAGER_FIRMWARE_ADDR + 0x121
        return original(name)

    backend.read_register = register
    monkeypatch.setattr(ram_recovery, "_ram_flasher_image", lambda: b"x" * 0x1000)
    monkeypatch.setattr(
        hw_lifecycle,
        "start_gnwmanager_verified",
        lambda *_a: pytest.fail("must not replace service"),
    )
    with pytest.raises(hw_lifecycle.TargetModeError, match="Recovery refused"):
        hw_lifecycle.recover_flasher(backend, gnw=object())
    assert backend.running


def test_passive_busy_mailbox_check_never_halts_target():
    backend = Backend(status=0xCAFE0001)
    state = hw_lifecycle.inspect_target_mode(backend)
    assert state["serviceState"] == "busy"
    assert not state["staleBusyMailbox"]
    assert backend.halts == backend.resumes == 0
