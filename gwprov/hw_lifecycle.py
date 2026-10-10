"""Safe hardware lifecycle checks shared by destructive GWProv operations."""
from __future__ import annotations

RAM_STUB_START = 0x24000000
RAM_STUB_END = 0x24100000
MAILBOX_STATUS = 0x24025800
VTOR = 0xE000ED08
DHCSR = 0xE000EDF0
STATUS_IDLE = 0xCAFE0000
S_HALT = 1 << 17


class TargetModeError(RuntimeError):
    """The target is in a service mode that must not be replaced implicitly."""


def inspect_target_mode(backend) -> dict:
    """Passively classify the RAM stub using the web-builder's live checks.

    VTOR identifies code executing from the SRAM window.  The mailbox IDLE
    value plus DHCSR.S_HALT clear is the stronger live-stub witness used by
    gnw-web-builder; the mailbox alone is stale after a reset or halt.
    """
    vtor = int(backend.read_uint32(VTOR)) & 0xFFFFFFFF
    dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
    status = int(backend.read_uint32(MAILBOX_STATUS)) & 0xFFFFFFFF
    stub_resident = RAM_STUB_START <= vtor < RAM_STUB_END
    halted = bool(dhcsr & S_HALT)
    alive = status == STATUS_IDLE and not halted
    detected = stub_resident or alive
    return {"mode": "gnwmanager-recovery" if detected else "application",
            "stubResident": stub_resident, "stubAlive": alive,
            "stubDetected": detected,
            "vtor": vtor, "dhcsr": dhcsr, "status": status,
            "halted": halted}


def assert_application_mode(backend, *, operation: str) -> dict:
    """Refuse to overwrite a resident RAM programmer from an unrelated run.

    The check must happen after attaching and before calling
    ``GnW.start_gnwmanager()``, which resets the device and overwrites the
    running RAM image.
    """
    state = inspect_target_mode(backend)
    if state["stubDetected"]:
        mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
        state_label = ("live and idle" if state["stubAlive"] else
                       f"resident but not confirmed idle (status 0x{state['status']:08x}, "
                       f"{'halted' if state['halted'] else 'running'})")
        raise TargetModeError(
            f"Cannot {operation}: gnwmanager Recovery Mode is {state_label} "
            f"(VTOR 0x{state['vtor']:08x}). GWProv will not reset or replace "
            "the RAM service implicitly. When its status is IDLE, run "
            "`gwprov device recover` to boot bank 1, then retry."
        )
    return state


def mark_backend_recovery_required(backend, phase: str) -> None:
    """Persist a no-poll guard across process exits for every lease key held."""
    leases = [getattr(backend, "_lease", None),
              *getattr(backend, "_probe_leases", [])]
    for lease in leases:
        if lease is not None:
            lease.mark_recovery_required(phase)


def clear_backend_recovery_required(backend) -> None:
    """Clear the durable no-poll guard only after the target is back in bank 1."""
    leases = [getattr(backend, "_lease", None),
              *getattr(backend, "_probe_leases", [])]
    for lease in leases:
        if lease is not None:
            lease.clear_recovery_required()


def recover_to_bank1(backend) -> dict:
    """Explicitly leave a quiescent RAM recovery stub and boot internal bank 1."""
    state = inspect_target_mode(backend)
    status = state["status"]
    terminal_error = (status & 0xFFFF0000) == 0xBAD00000
    safe_to_reset = (status == STATUS_IDLE or terminal_error or
                     (status == 0 and not state["stubResident"]))
    if not safe_to_reset:
        raise TargetModeError(
            "Recovery refused: the device is not in a confirmed quiescent gnwmanager "
            f"Recovery Mode (VTOR 0x{state['vtor']:08x}, status "
            f"0x{state['status']:08x}, "
            f"{'halted' if state['halted'] else 'running'}). "
            "Wait for an active ERASE/PROG/HASH operation to finish; if the "
            "state cannot be read, restore the device connection before retrying."
        )
    backend.reset_and_halt()
    msp = backend.read_uint32(0x08000000)
    pc = backend.read_uint32(0x08000004)
    backend.write_register("msp", msp)
    backend.write_register("pc", pc)
    backend.resume()
    after = inspect_target_mode(backend)
    pc_after = int(backend.read_register("pc")) & ~1
    if after["halted"] or after["stubResident"] or RAM_STUB_START <= pc_after < RAM_STUB_END:
        raise TargetModeError(
            "Bank 1 was selected but the CPU did not leave Recovery Mode; "
            "check `gwprov ps` before issuing another hardware command."
        )
    clear_backend_recovery_required(backend)
    return {"msp": msp, "pc": pc, "pcAfter": pc_after, **after}

