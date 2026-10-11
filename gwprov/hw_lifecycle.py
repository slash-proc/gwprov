"""Safe hardware lifecycle checks shared by destructive GWProv operations."""
from __future__ import annotations

import time

RAM_STUB_START = 0x24000000
RAM_STUB_END = 0x24100000
MAILBOX_STATUS = 0x24025800
VTOR = 0xE000ED08
DHCSR = 0xE000EDF0
STATUS_IDLE = 0xCAFE0000
S_HALT = 1 << 17
GNWMANAGER_CONTEXT_COUNTER = 0x20000054


class TargetModeError(RuntimeError):
    """The target is in a service mode that must not be replaced implicitly."""


def inspect_target_mode(backend) -> dict:
    """Passively classify the RAM stub using the web-builder's live checks.

    VTOR identifies code executing from the SRAM window. The mailbox IDLE
    value and DHCSR.S_HALT clear only confirm liveness after VTOR identifies
    the RAM service; the mailbox alone is stale after a reset or halt.
    """
    vtor = int(backend.read_uint32(VTOR)) & 0xFFFFFFFF
    dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
    status = int(backend.read_uint32(MAILBOX_STATUS)) & 0xFFFFFFFF
    stub_resident = RAM_STUB_START <= vtor < RAM_STUB_END
    halted = bool(dhcsr & S_HALT)
    # The mailbox is retained across resets. IDLE plus a running core is not
    # sufficient on its own: application firmware can be running with a stale
    # IDLE mailbox, which would make us attach to the app and claim recovery
    # succeeded. Require the vector table to identify the RAM service too.
    alive = stub_resident and status == STATUS_IDLE and not halted
    detected = stub_resident
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
            "`gwprov device recover` to synchronize with the flasher, then retry."
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


def attach_to_idle_gnwmanager(backend, gnw, *, timeout: float = 5.0) -> int:
    """Synchronize a host GnW client with an already-running idle RAM service.

    The service's context counter is firmware state, not host state. Reusing a
    live stub with a fresh GnW instance requires adopting that counter before
    posting mailbox work, as gnw-web-builder does when attaching its flasher.
    """
    ready_addresses = [int(context["ready"].address) for context in gnw.contexts]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status_before = backend.read_uint32(MAILBOX_STATUS) & 0xFFFFFFFF
        ready_before = [backend.read_uint32(addr) & 0xFFFFFFFF for addr in ready_addresses]
        upload_before = gnw.read_uint32("upload_in_progress") & 0xFFFFFFFF
        download_before = gnw.read_uint32("download_in_progress") & 0xFFFFFFFF
        if (status_before == STATUS_IDLE and ready_before == [0, 0]
                and upload_before == 0 and download_before == 0):
            counter = backend.read_uint32(GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            status_after = backend.read_uint32(MAILBOX_STATUS) & 0xFFFFFFFF
            ready_after = [backend.read_uint32(addr) & 0xFFFFFFFF for addr in ready_addresses]
            upload_after = gnw.read_uint32("upload_in_progress") & 0xFFFFFFFF
            download_after = gnw.read_uint32("download_in_progress") & 0xFFFFFFFF
            counter_after = backend.read_uint32(GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            if (status_after == STATUS_IDLE and ready_after == [0, 0]
                    and upload_after == 0 and download_after == 0
                    and counter != 0 and counter_after == counter):
                gnw.context_counter = counter
                gnw._gnwmanager_started = True
                return counter
        time.sleep(0.02)
    raise TargetModeError(
        "Cannot attach to the running gnwmanager RAM service: its mailbox did not "
        "reach a stable, idle state. No flash commands were sent."
    )


def recover_flasher(backend, gnw=None) -> dict:
    """Start or synchronize with the RAM flasher, leaving it ready for commands."""
    if gnw is None:
        from gnwmanager.gnw import GnW
        gnw = GnW(backend)

    state = inspect_target_mode(backend)
    if state["stubAlive"]:
        counter = attach_to_idle_gnwmanager(backend, gnw)
        mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
        return {**state, "contextCounter": counter, "started": False}
    terminal_error = (state["status"] & 0xFFFF0000) == 0xBAD00000
    recoverable_resident_stub = (state["stubResident"] and
                                 state["status"] == STATUS_IDLE and state["halted"])
    if state["stubDetected"] and not (terminal_error or recoverable_resident_stub):
        raise TargetModeError(
            "Recovery refused: gnwmanager is resident but not confirmed live and idle "
            f"(VTOR 0x{state['vtor']:08x}, status 0x{state['status']:08x}, "
            f"{'halted' if state['halted'] else 'running'}). "
            "Wait for any active operation to finish; do not reset a busy or halted flasher."
        )
    if not state["stubDetected"]:
        assert_application_mode(backend, operation="start the recovery flasher")
    mark_backend_recovery_required(backend, "starting recovery flasher")
    gnw.start_gnwmanager(force=bool(state["stubDetected"]))
    state = inspect_target_mode(backend)
    if not state["stubAlive"]:
        raise TargetModeError(
            "Recovery failed: gnwmanager did not reach live IDLE after startup "
            f"(VTOR 0x{state['vtor']:08x}, status 0x{state['status']:08x})."
        )
    counter = attach_to_idle_gnwmanager(backend, gnw)
    mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
    state = inspect_target_mode(backend)
    return {**state, "contextCounter": counter, "started": True}

