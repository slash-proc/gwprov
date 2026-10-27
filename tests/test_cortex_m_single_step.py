import pytest
from gwprov import debug_shell

RemoteGDBBackend = debug_shell.RemoteGDBBackend


class FakeDHCSR:
    def __init__(self, *, auto_step=True, auto_rehalt=True):
        self.controls = (
            debug_shell._CORTEX_M_C_DEBUGEN
            | debug_shell._CORTEX_M_C_HALT
            | debug_shell._CORTEX_M_C_SNAPSTALL
        )
        self.status = debug_shell._CORTEX_M_S_HALT | debug_shell._CORTEX_M_S_REGRDY
        self.auto_step = auto_step
        self.auto_rehalt = auto_rehalt
        self.pending_step = False
        self.writes = []
        self.reads = []

    @property
    def raw(self):
        return self.controls | self.status

    def read_memory(self, address, size):
        assert address == debug_shell._CORTEX_M_DHCSR and size == 4
        if self.pending_step and self.auto_step:
            self.pending_step = False
            self.controls &= ~debug_shell._CORTEX_M_C_STEP
            self.controls |= debug_shell._CORTEX_M_C_HALT
            self.status |= debug_shell._CORTEX_M_S_HALT | debug_shell._CORTEX_M_S_REGRDY
        value = self.raw
        self.reads.append(value)
        return value.to_bytes(4, "little")

    def write_memory(self, address, data):
        assert address == debug_shell._CORTEX_M_DHCSR and len(data) == 4
        command = int.from_bytes(data, "little")
        assert command & 0xFFFF0000 == debug_shell._CORTEX_M_DHCSR_KEY
        controls = command & debug_shell._CORTEX_M_DHCSR_CONTROL_MASK
        self.writes.append(command)
        if controls & debug_shell._CORTEX_M_C_STEP:
            self.pending_step = True
            self.controls = controls & ~debug_shell._CORTEX_M_C_HALT
            self.status &= ~debug_shell._CORTEX_M_S_HALT
        elif controls & debug_shell._CORTEX_M_C_HALT:
            self.controls = controls
            if self.auto_rehalt:
                self.status |= debug_shell._CORTEX_M_S_HALT | debug_shell._CORTEX_M_S_REGRDY
            else:
                self.status &= ~debug_shell._CORTEX_M_S_HALT
        else:
            self.controls = controls
            self.status &= ~debug_shell._CORTEX_M_S_HALT


def backend_for(profile, transport=None):
    backend = RemoteGDBBackend("example.invalid", 3333, daemon_managed=True, target_profile=profile)
    backend._is_running = False
    return backend


def test_profiled_cortex_m_step_uses_dhcsr_and_preserves_controls():
    backend = backend_for("stm32h7b0")
    fake = FakeDHCSR()
    backend.read_memory = fake.read_memory
    backend.write_memory = fake.write_memory

    assert backend.single_step() == b"S05"
    assert fake.writes == [
        debug_shell._CORTEX_M_DHCSR_KEY
        | debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_HALT
        | debug_shell._CORTEX_M_C_SNAPSTALL
        | debug_shell._CORTEX_M_C_MASKINTS,
        debug_shell._CORTEX_M_DHCSR_KEY
        | debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_STEP
        | debug_shell._CORTEX_M_C_SNAPSTALL
        | debug_shell._CORTEX_M_C_MASKINTS,
        debug_shell._CORTEX_M_DHCSR_KEY
        | debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_HALT
        | debug_shell._CORTEX_M_C_SNAPSTALL,
    ]
    assert backend._is_running is False
    assert fake.raw & debug_shell._CORTEX_M_S_HALT


def test_cortex_m_step_timeout_rehalts_restores_and_reports(monkeypatch):
    from gwprov import debug_shell

    backend = backend_for("stm32h7b0")
    fake = FakeDHCSR(auto_step=False)
    backend.read_memory = fake.read_memory
    backend.write_memory = fake.write_memory
    monkeypatch.setattr(debug_shell, "_CORTEX_M_STEP_TIMEOUT_S", 0.01)
    ticks = iter((0.0, 0.1, 0.2, 0.3))
    monkeypatch.setattr(debug_shell.time, "monotonic", lambda: next(ticks, 1.0))
    monkeypatch.setattr(debug_shell.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="single-step failed; debug controls restored") as exc:
        backend.single_step()
    assert "timed out waiting" in str(exc.value)
    assert "timeout_rehalt" in str(exc.value)
    assert fake.writes[-2] == (
        debug_shell._CORTEX_M_DHCSR_KEY
        | debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_HALT
        | debug_shell._CORTEX_M_C_SNAPSTALL
        | debug_shell._CORTEX_M_C_MASKINTS
    )
    assert fake.writes[-1] == (
        debug_shell._CORTEX_M_DHCSR_KEY
        | debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_HALT
        | debug_shell._CORTEX_M_C_SNAPSTALL
    )
    assert fake.raw & debug_shell._CORTEX_M_S_HALT
    assert fake.raw & debug_shell._CORTEX_M_DHCSR_CONTROL_MASK == (
        debug_shell._CORTEX_M_C_DEBUGEN
        | debug_shell._CORTEX_M_C_HALT
        | debug_shell._CORTEX_M_C_SNAPSTALL
    )
    assert backend._is_running is False


def test_cortex_m_timeout_without_confirmed_rehalt_marks_state_unknown(monkeypatch):
    from gwprov import debug_shell

    backend = backend_for("stm32h7b0")
    fake = FakeDHCSR(auto_step=False, auto_rehalt=False)
    backend.read_memory = fake.read_memory
    backend.write_memory = fake.write_memory
    monkeypatch.setattr(debug_shell, "_CORTEX_M_STEP_TIMEOUT_S", 0.01)
    ticks = iter((0.0, 0.1, 0.2, 0.3))
    monkeypatch.setattr(debug_shell.time, "monotonic", lambda: next(ticks, 1.0))
    monkeypatch.setattr(debug_shell.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="cleanup could not be verified"):
        backend.single_step()
    assert backend._is_running is None
    assert not fake.raw & debug_shell._CORTEX_M_S_HALT


def test_unknown_architecture_keeps_generic_rsp_step(monkeypatch):
    backend = backend_for(None)
    sent = []
    backend._write = lambda data: sent.append(data)
    backend._read = lambda _size: b"+"
    backend._wait_for_packet = lambda: b"T05"

    assert backend.single_step() == b"T05"
    assert sent == [b"$s#73"]
    assert backend._is_running is False
