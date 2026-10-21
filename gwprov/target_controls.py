"""Capability-based target controls shared by device backends.

Transport access alone does not identify peripheral registers. Controls are
registered against an explicit target profile so a remote GDB endpoint never
gets probed at guessed MMIO addresses. New target controls should register a
handler here and expose their state/actions through the same API and CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .hw_lifecycle import DBGMCU_APB3FZ1, DBGMCU_APB4FZ1, DBG_IWDG1, DBG_WWDG1


@dataclass(frozen=True)
class WatchdogProfile:
    name: str
    registers: tuple[tuple[str, int, int], ...]
    runtime_state_registers: tuple[tuple[str, int, int], ...]
    runtime_state_unknown: tuple[str, ...]
    runtime_enable_detail: str
    runtime_detail: str
    freeze_detail: str


class TargetControl(Protocol):
    """One named family of safe, target-specific controls.

    Handlers report each independently controllable facet. A facet's ``set``
    action is only offered when the handler can actually perform it.
    """

    name: str
    facet_names: tuple[str, ...]

    def status(self, session, target_profile: str | None) -> dict: ...

    def set(self, session, facet: str, enabled: bool, target_profile: str | None) -> dict: ...


_CONTROL_REGISTRY: dict[str, dict[str, TargetControl]] = {}


def register_target_control(
    target_profile: str, handler: TargetControl, *, replace: bool = False
) -> None:
    """Register a reusable control handler for a known target profile.

    Registration is intentionally explicit: discovering a transport or a
    backend's memory access methods is not enough to infer a target register
    map or claim that a control is safe.
    """
    profile = target_profile.casefold()
    controls = _CONTROL_REGISTRY.setdefault(profile, {})
    if handler.name in controls and not replace:
        raise ValueError(f"target control already registered: {profile}/{handler.name}")
    controls[handler.name] = handler


def registered_control_names() -> tuple[str, ...]:
    return tuple(sorted({name for controls in _CONTROL_REGISTRY.values() for name in controls}))


def registered_target_profiles() -> tuple[str, ...]:
    """Return target profiles that advertise at least one control."""
    return tuple(sorted(profile for profile, controls in _CONTROL_REGISTRY.items() if controls))


def control_status(session, name: str, target_profile: str | None) -> dict:
    """Report one control family without probing an unidentified target."""
    control_name = name.casefold()
    if control_name not in registered_control_names():
        known = ", ".join(registered_control_names()) or "none"
        raise ValueError(f"unknown target control {name!r}; registered controls: {known}")
    handler = _handler_for(session, control_name, target_profile)
    return handler.status(session, target_profile)


def controls_status(session, target_profile: str | None) -> dict:
    """Report all registered control families for a selected target."""
    profile = (target_profile or "").casefold()
    target_name = profile or "unspecified"
    result = {"target": target_name, "controls": {}}
    for name in registered_control_names():
        handler = _handler_for(session, name, target_profile)
        result["controls"][name] = handler.status(session, target_profile)
    return result


def set_control(
    session,
    name: str,
    facet: str,
    enabled: bool,
    target_profile: str | None,
) -> dict:
    """Set a named, registered capability or fail with its support reason."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    control_name = name.casefold()
    if control_name not in registered_control_names():
        known = ", ".join(registered_control_names()) or "none"
        raise ValueError(f"unknown target control {name!r}; registered controls: {known}")
    handler = _handler_for(session, control_name, target_profile)
    current = handler.status(session, target_profile)
    selected = current.get("facets", {}).get(facet.casefold())
    if selected is None:
        raise ValueError(f"unknown {control_name} control facet {facet!r}")
    action = "enable" if enabled else "disable"
    action_status = selected.get("actions", {}).get(action)
    if action_status is not None:
        if not action_status.get("supported", False):
            raise ValueError(action_status.get("detail") or selected.get("detail") or "unsupported")
    elif not selected.get("supported", False):
        raise ValueError(selected.get("detail", "control facet is unsupported"))
    return handler.set(session, facet.casefold(), enabled, target_profile)


def _handler_for(session, name: str, target_profile: str | None) -> TargetControl:
    transport = getattr(session, "transport", None)
    # An explicitly registered modeled target takes precedence over its
    # transport. An explicit target profile always wins, so a remote GDB
    # endpoint is never probed based on transport alone.
    profile = (target_profile or ("gwemu" if transport == "gwemu" else "")).casefold()
    handler = _CONTROL_REGISTRY.get(profile, {}).get(name)
    if handler is not None:
        return handler
    if transport == "gwemu":
        detail = (
            "GWemu does not model WWDG/IWDG countdown or reset behavior; they are "
            "register-only stubs."
            if name == "watchdog"
            else "GWemu does not model this target control."
        )
        return _UnsupportedControl(
            name,
            "not-modeled",
            detail,
            "gwemu",
            ("runtime", "halt_freeze"),
        )
    reason = (
        f"No target-control profile is registered for {target_profile!r}; "
        "GWProv did not probe target MMIO."
        if target_profile
        else "No target-control profile is selected; GWProv did not probe target MMIO."
    )
    templates = [controls[name] for controls in _CONTROL_REGISTRY.values() if name in controls]
    facet_names = getattr(templates[0], "facet_names", ("runtime", "halt_freeze"))
    return _UnsupportedControl(
        name, "unsupported", reason, target_profile or "unspecified", facet_names
    )


@dataclass(frozen=True)
class _UnsupportedControl:
    name: str
    state: str
    detail: str
    target: str
    facet_names: tuple[str, ...]

    def _result(self) -> dict:
        facets = {
            name: {
                "status": self.state,
                "supported": False,
                "detail": self.detail,
                "actions": {
                    "enable": {"status": self.state, "supported": False, "detail": self.detail},
                    "disable": {"status": self.state, "supported": False, "detail": self.detail},
                },
            }
            for name in self.facet_names
        }
        return {
            "name": self.name,
            "status": self.state,
            "supported": False,
            "target": self.target,
            "facets": facets,
            "runtime_control": facets.get("runtime"),
            "halt_freeze": facets.get("halt_freeze"),
        }

    def status(self, session, target_profile: str | None) -> dict:
        return self._result()

    def set(self, session, facet: str, enabled: bool, target_profile: str | None) -> dict:
        result = self._result()
        state = result["facets"].get(facet)
        if state is None:
            raise ValueError(f"unknown {self.name} control facet {facet!r}")
        raise ValueError(state["detail"])


class _STM32H7B0WatchdogControl:
    name = "watchdog"
    facet_names = ("runtime", "halt_freeze")

    def status(self, session, target_profile: str | None) -> dict:
        backend = session.backend
        can_read = callable(getattr(backend, "read_memory", None))
        can_write = callable(getattr(backend, "write_memory", None))
        profile = WATCHDOG_PROFILES["stm32h7b0"]
        runtime = {
            "supported": False,
            "status": "not-controllable",
            "enabled": None,
            "detail": WATCHDOG_PROFILES["stm32h7b0"].runtime_detail,
            "actions": {
                "enable": {
                    "supported": False,
                    "status": "not-controllable",
                    "detail": WATCHDOG_PROFILES["stm32h7b0"].runtime_enable_detail,
                },
                "disable": {
                    "supported": False,
                    "status": "not-controllable",
                    "detail": WATCHDOG_PROFILES["stm32h7b0"].runtime_detail,
                },
            },
        }
        runtime["state"] = {name: None for name in profile.runtime_state_unknown}
        for name, address, mask in profile.runtime_state_registers:
            try:
                value = session.u32(address) if can_read else 0xFFFFFFFF
                runtime["state"][name] = None if value == 0xFFFFFFFF else bool(value & mask)
            except Exception:
                runtime["state"][name] = None
        runtime["state_detail"] = (
            "WWDG activation is read from its WDGA bit. IWDG1 exposes no readable "
            "started bit, so its runtime state is unknown."
        )
        freeze = {
            "supported": can_read and can_write,
            "status": (
                "available"
                if can_read and can_write
                else "read-only" if can_read else "unsupported"
            ),
            "detail": (
                WATCHDOG_PROFILES["stm32h7b0"].freeze_detail
                if can_read and can_write
                else (
                    "Backend cannot read watchdog freeze registers."
                    if not can_read
                    else "Backend can read but cannot write watchdog freeze registers."
                )
            ),
            "actions": {
                action: {
                    "supported": can_read and can_write,
                    "status": (
                        "available"
                        if can_read and can_write
                        else "read-only" if can_read else "unsupported"
                    ),
                    "detail": (
                        WATCHDOG_PROFILES["stm32h7b0"].freeze_detail
                        if can_read and can_write
                        else (
                            "Backend cannot read watchdog freeze registers."
                            if not can_read
                            else "Backend can read but cannot write watchdog freeze registers."
                        )
                    ),
                }
                for action in ("enable", "disable")
            },
            "state": {},
        }
        for name, address, mask in WATCHDOG_PROFILES["stm32h7b0"].registers:
            try:
                value = session.u32(address) if can_read else 0xFFFFFFFF
            except Exception as error:
                value = 0xFFFFFFFF
                freeze["supported"] = False
                freeze["status"] = "unavailable"
                freeze["detail"] = f"Cannot read target watchdog freeze state: {error}"
                for action in freeze["actions"].values():
                    action["supported"] = False
                    action["status"] = "unavailable"
                    action["detail"] = freeze["detail"]
            enabled = None if value == 0xFFFFFFFF else bool(value & mask)
            freeze["state"][name] = enabled
            # Keep the legacy fields during migration to the generic API.
            freeze[name] = {"enabled": enabled, "available": enabled is not None}
        return {
            "name": self.name,
            "target": "STM32H7B0",
            "supported": runtime["supported"] or freeze["supported"],
            "facets": {"runtime": runtime, "halt_freeze": freeze},
            # Backwards-compatible response keys for existing Python clients.
            "runtime_control": runtime,
            "halt_freeze": freeze,
        }

    def set(self, session, facet: str, enabled: bool, target_profile: str | None) -> dict:
        status = self.status(session, target_profile)
        selected = status["facets"].get(facet)
        if selected is None:
            raise ValueError(
                f"unknown watchdog control facet {facet!r}; use runtime or halt_freeze"
            )
        if not selected["supported"]:
            raise ValueError(selected["detail"])
        if facet == "runtime":
            # The handler must never reinterpret debug-freeze as runtime off.
            raise ValueError(selected["detail"])
        if facet != "halt_freeze":
            raise ValueError(f"unsupported watchdog control facet {facet!r}")
        profile = WATCHDOG_PROFILES["stm32h7b0"]
        for name, address, mask in profile.registers:
            before = session.u32(address)
            if before == 0xFFFFFFFF:
                raise RuntimeError(f"cannot read watchdog freeze register for {name}")
            after = before | mask if enabled else before & ~mask
            if after != before:
                session.write_u32(address, after)
            verified = session.u32(address)
            if verified == 0xFFFFFFFF or bool(verified & mask) != enabled:
                action = "enable" if enabled else "disable"
                raise RuntimeError(f"watchdog freeze for {name} did not {action}")
        return self.status(session, target_profile)


WATCHDOG_PROFILES: dict[str, WatchdogProfile] = {
    "stm32h7b0": WatchdogProfile(
        name="STM32H7B0",
        registers=(
            ("WWDG1", DBGMCU_APB3FZ1, DBG_WWDG1),
            ("IWDG1", DBGMCU_APB4FZ1, DBG_IWDG1),
        ),
        runtime_state_registers=(("WWDG1", 0x50003000, 1 << 7),),
        runtime_state_unknown=("IWDG1",),
        runtime_detail=(
            "STM32H7B0 watchdogs cannot be stopped through a safe runtime debugger "
            "control once started; reset is required."
        ),
        runtime_enable_detail=(
            "Starting an STM32H7B0 watchdog is irreversible until reset and can reset the "
            "target; GWProv does not expose a generic runtime-start operation."
        ),
        freeze_detail=(
            "DBGMCU freeze pauses watchdog counters only while the CPU is halted; "
            "it does not disable them while firmware runs."
        ),
    ),
}


def watchdog_status(session, target_profile: str | None) -> dict:
    """Compatibility wrapper for the generic watchdog control family."""
    return control_status(session, "watchdog", target_profile)


def set_watchdog_freeze(session, enabled: bool, target_profile: str | None) -> dict:
    """Compatibility wrapper for the generic watchdog halt-freeze facet."""
    return set_control(session, "watchdog", "halt_freeze", enabled, target_profile)


register_target_control("stm32h7b0", _STM32H7B0WatchdogControl())
