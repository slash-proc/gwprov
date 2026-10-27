"""Safe hardware lifecycle checks shared by destructive GWProv operations."""

from __future__ import annotations

import hashlib
import re
import time

RAM_STUB_START = 0x24000000
RAM_STUB_END = 0x24100000
MAILBOX_STATUS = 0x24025800
VTOR = 0xE000ED08
DHCSR = 0xE000EDF0
STATUS_IDLE = 0xCAFE0000
GNWMANAGER_STATUS_NAMES = {
    0x00000000: "BOOTING",
    0xBAD00001: "BAD_HASH_RAM",
    0xBAD00002: "BAD_HASH_FLASH",
    0xBAD00003: "NOT_ALIGNED",
    0xBAD00004: "BAD_DECOMPRESS",
    0xBAD00005: "BAD_SEGFAULT",
    0xBAD00006: "BAD_FLASH_COMM",
    0xBAD00007: "BAD_SD_FS_MOUNT",
    0xBAD00008: "BAD_SD_OPEN",
    0xBAD00009: "BAD_SD_WRITE",
    0xBAD0000A: "BAD_SD_UNLINK",
    0xBAD0000B: "BAD_SD_DIR",
    0xBAD0000C: "BAD_SD_LIST_TRUNC",
    0xBAD0000D: "BAD_SD_READ",
    0xBAD0000E: "BAD_HASH_RAM_COMPRESSED",
    0xCAFE0000: "IDLE",
    0xCAFE0001: "ERASE",
    0xCAFE0002: "PROGRAM",
    0xCAFE0003: "HASH",
}
S_HALT = 1 << 17
# Match gnw-web-builder's STM32H7B0 lifecycle. Retro-Go's WWDG1 and
# gnwmanager's IWDG1 keep counting while the core is halted unless the DBGMCU
# freeze bits are set first. Either can reset the target during a RAM-stub load.
DBGMCU_APB3FZ1 = 0x5C001034
DBG_WWDG1 = 1 << 6
DBGMCU_APB4FZ1 = 0x5C001054
DBG_IWDG1 = 1 << 18
# gnwmanager's DTCM context_counter symbol; attach_flashing synchronizes to it.
GNWMANAGER_CONTEXT_COUNTER = 0x20000054
GNWMANAGER_FIRMWARE_ADDR = 0x240E6800
GNWMANAGER_ACTIVE_STATUSES = {0xCAFE0001, 0xCAFE0002, 0xCAFE0003}
VTOR_BANK1_START = 0x08000000
VTOR_BANK2_START = 0x08100000
VTOR_BANK_END = 0x08200000
BOOTLOADER_START = 0x08032000
BOOTLOADER_END = 0x08040000
RETROGO_VERSION_WINDOW = 0x00030000
# gnw-web-builder/packages/gnw-flasher/src/index.ts, STOCK_MODELS. These are
# SHA-1 fingerprints of the residual ITCM image used by gnwmanager autodetect.
STOCK_ITCM_SIGNATURES = {
    "Mario": (0x00, "ca71a54c0a22cca5c6ee129faee9f99f3a346ca0"),
    "Zelda": (0x20, "2f70156235ffd871599facf64457040d549353b4"),
}
OFW_INITIAL_SP = {0x20011330: "Mario", 0x2001B620: "Zelda"}
OFW_IMAGE_SIZE = 128 * 1024
INTERNAL_BANK_SIZE = 256 * 1024
FLASH_OPTSR_CUR = 0x5200201C
DUAL_BOOTLOADER_HASHES = {
    (49628, "d657200fb56197cfd0e1c3ed14f953303f163a309e76a0d4e92d506970bd5c74"),
    (49632, "bb373203371e21426fd1068d5d73c81411fd07c085b3570dab271a6ea78f8e28"),
    (49676, "ac55c3e5ed4c7da1cefca399a6497ec6843953e1fb7b6bc3ce3d9a508d24458e"),
    (49724, "93b5339fded692750ab3d897f1e0236f0339a6c81bfc21bc3a6b62a4f38ac357"),
    (49796, "4b8661e833a24e57fdc12ae342a2b39188ace7d127063526104b5a01f24a5a9c"),
    (50248, "43fd718c1a53dda140f29e4025fc8b6d3bfa71f19a710139135f998e4de022d7"),
}
STANDALONE_BOOTLOADER_HASHES = {
    (59208, "d683f53fc82742a7ff0b757fb0e9d87f3ee46df89506146df53b4492ea5a131e"),
    (67828, "72354bc2b80697532e945dd3788d0336a45cf6b71fa27ed991b8adf6db439d77"),
    (67888, "30ce2e56c52571a612a0276fe95ddf398bf22c729f02406a7c8c623866a90bbd"),
    (49628, "f88a621a6261742fdff09a891ad984675b95e3f1bc435ab365a7537f5f9ee0e4"),
    (49632, "f830cb28ac6896d6d959afb8a453041f5688a47da01db86e74bb731e1c25acf9"),
    (49676, "1c66ddb0649931607295542f97bf4f37177a2f4ae5a732834b97cbcd0d3c4097"),
    (49724, "12524ccefeb5d016d231594c4be50f47de22115361c673378c17385ec93b5af1"),
    (49780, "be394de38f5d69baf68aa9d29e77c8697b7bbac5ba01138d09c9b8c22f2153f2"),
    (50232, "fe171fad5160c9a02edab5c435f64fc4ee19e97fcf684abe2fe72a54903d005b"),
}


class TargetModeError(RuntimeError):
    """The target is in a service mode that must not be replaced implicitly."""


def inspect_target_mode(backend, *, verify_flash_vtor_mailbox: bool = False) -> dict:
    """Classify the recovery service, optionally verifying a flash-VTOR mailbox.

    A stale IDLE word survives watchdog resets, so it is not proof of a live
    service when VTOR points into flash. Callers about to attach/reset may opt
    into a brief PC snapshot; passive callers never halt the target.
    """
    vtor = int(backend.read_uint32(VTOR)) & 0xFFFFFFFF
    dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
    status = int(backend.read_uint32(MAILBOX_STATUS)) & 0xFFFFFFFF
    try:
        option_status = int(backend.read_uint32(FLASH_OPTSR_CUR)) & 0xFFFFFFFF
        locked = None if option_status == 0xFFFFFFFF else ((option_status >> 8) & 0xFF) != 0xAA
    except Exception:
        locked = None
    stub_resident = RAM_STUB_START <= vtor < RAM_STUB_END
    halted = bool(dhcsr & S_HALT)
    mailbox_alive = status == STATUS_IDLE and not halted
    mailbox_idle_candidate = mailbox_alive and not stub_resident
    # An idle mailbox is authoritative only with the RAM service VTOR. If a
    # watchdog reset left the old IDLE word behind, inspect the PC only for
    # lifecycle callers that explicitly intend to attach or replace the stub.
    service_state = _classify_gnwmanager_state(
        status=status, halted=halted, mailbox_alive=mailbox_alive
    )
    pc = None
    pc_verification = None
    busy_candidate = status in GNWMANAGER_ACTIVE_STATUSES and not stub_resident
    stale_busy = False
    if mailbox_idle_candidate or (busy_candidate and verify_flash_vtor_mailbox):
        if verify_flash_vtor_mailbox:
            try:
                from .ram_recovery import _ram_flasher_image

                image = _ram_flasher_image()
                pc, _ = _read_pc_snapshot(backend, halted)
                pc_address = pc & ~1
                pc_verification = (
                    GNWMANAGER_FIRMWARE_ADDR <= pc_address < GNWMANAGER_FIRMWARE_ADDR + len(image)
                )
                if busy_candidate and pc_verification is False:
                    # An interrupted write can leave ERASE/PROGRAM/HASH in SRAM
                    # after reboot into flash. Do not infer staleness from one
                    # PC outside the stub: require unlocked flash execution on
                    # two snapshots and idle controllers, with stable metadata.
                    flash_pc = VTOR_BANK1_START <= pc_address < VTOR_BANK_END
                    flash_vtor = VTOR_BANK1_START <= vtor < VTOR_BANK_END
                    if locked is False and flash_pc and flash_vtor and not halted:
                        pc2, _ = _read_pc_snapshot(backend, False)
                        sr1 = int(backend.read_uint32(0x52002010)) & 0xFFFFFFFF
                        sr2 = int(backend.read_uint32(0x52002110)) & 0xFFFFFFFF
                        stale_busy = (
                            VTOR_BANK1_START <= (pc2 & ~1) < VTOR_BANK_END
                            and sr1 != 0xFFFFFFFF
                            and sr2 != 0xFFFFFFFF
                            and not ((sr1 | sr2) & 7)  # BSY, WBNE, QW
                            and int(backend.read_uint32(VTOR)) == vtor
                            and int(backend.read_uint32(MAILBOX_STATUS)) == status
                        )
                    if not stale_busy:
                        pc_verification = None
            except Exception:
                # Unknown is not proof of staleness: preserve the protective
                # resident-service classification and refuse implicit reset.
                pc_verification = None
            if pc_verification is False:
                service_state = None
            elif pc_verification is None:
                service_state = "unverified"
        else:
            service_state = None
    alive = service_state == "idle"
    detected = stub_resident or service_state is not None
    return {
        "mode": "gnwmanager-recovery" if detected else "application",
        "stubResident": stub_resident,
        "stubAlive": alive,
        "stubDetected": detected,
        "locked": locked,
        "serviceState": service_state,
        "vtor": vtor,
        "dhcsr": dhcsr,
        "status": status,
        "halted": halted,
        "mailboxIdleCandidate": mailbox_idle_candidate,
        "mailboxPc": pc,
        "mailboxPcVerified": pc_verification,
        "staleBusyMailbox": stale_busy,
    }


def identify_runtime(
    backend,
    *,
    start_locked_stub: bool = False,
    cached_banks: list[dict] | None = None,
    pc_fallback: bool = False,
) -> dict:
    """Passively identify the currently selected firmware/runtime.

    This mirrors gnw-web-builder's VTOR classifier and stock ITCM fingerprints.
    Normally this is passive. ``start_locked_stub`` opts into the builder's
    locked-device attach behavior and starts the verified RAM flasher. Call
    only from the daemon's serialized monitor when no foreground target operation owns it.
    """
    # Match the builder's first-wave order: residual ITCM fingerprint and the RDP
    # option byte establish the stock variant/protection before any bank scan.
    stock_model = _stock_itcm_model(backend)
    try:
        option_status = int(backend.read_uint32(FLASH_OPTSR_CUR)) & 0xFFFFFFFF
        locked = None if option_status == 0xFFFFFFFF else ((option_status >> 8) & 0xFF) != 0xAA
    except Exception:
        locked = None
    try:
        status = int(backend.read_uint32(MAILBOX_STATUS)) & 0xFFFFFFFF
    except Exception:
        status = None
    try:
        dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
        halted = bool(dhcsr & S_HALT)
    except Exception:
        dhcsr = None
        halted = None
    vtor = int(backend.read_uint32(VTOR)) & 0xFFFFFFFF
    stub_resident = RAM_STUB_START <= vtor < RAM_STUB_END
    mailbox_alive = status == STATUS_IDLE and halted is False
    # The builder's mailbox probe identifies a live service even when it leaves
    # VTOR in flash. Unlocked runtime naming below still follows VTOR, so a
    # ready mailbox cannot replace the visible stock/Retro-Go identity.
    service_state = _classify_gnwmanager_state(
        status=status, halted=halted, mailbox_alive=mailbox_alive
    )
    stub_alive = service_state == "idle"
    confirmed_stub = service_state is not None
    unlocked_flash_runtime = locked is False and (
        VTOR_BANK1_START <= vtor < VTOR_BANK2_START or VTOR_BANK2_START <= vtor < VTOR_BANK_END
    )
    state = {
        "mode": (
            "gnwmanager-recovery"
            if stub_resident or (confirmed_stub and not unlocked_flash_runtime)
            else "application"
        ),
        "stubResident": stub_resident,
        "stubAlive": stub_alive,
        "stubDetected": stub_resident or confirmed_stub,
        "serviceState": service_state,
        "vtor": vtor,
        "dhcsr": dhcsr,
        "status": status,
        "halted": halted,
    }
    result = {
        "application": "Unknown",
        "detail": None,
        "installed": "Unknown",
        "banks": [],
        **state,
    }
    result["locked"] = locked
    if service_state in {"busy", "stalled"}:
        if locked is True:
            result["installed"] = "Locked: firmware scan unavailable"
        elif cached_banks is not None:
            result.update(
                banks=cached_banks,
                installed=(
                    "; ".join(item["image"] for item in cached_banks if item["image"] != "Erased")
                    or "No firmware detected"
                ),
            )
        else:
            result["installed"] = "Internal flash scan deferred while gnwmanager is active"
        if service_state == "busy":
            service_status = GNWMANAGER_STATUS_NAMES.get(
                status, f"0x{status:08x}" if isinstance(status, int) else "unknown"
            )
            detail = f"gnwmanager reports {service_status}; internal flash scan deferred"
        else:
            service_status = GNWMANAGER_STATUS_NAMES.get(
                status, f"0x{status:08x}" if isinstance(status, int) else "unknown"
            )
            detail = (
                f"gnwmanager reports {service_status} while the core is halted; "
                "internal flash scan deferred"
            )
        result.update(application="gnwmanager Recovery stub", detail=detail)
        return result
    if locked is True and not state["stubDetected"] and start_locked_stub:
        # This is the web-builder locked-device attach behavior: a locked target
        # cannot be scanned, so start the RAM flasher immediately. Persist the
        mark_backend_recovery_required(backend, "starting recovery flasher for locked device")
        try:
            from gnwmanager.gnw import GnW

            gnw = GnW(backend)
            start_gnwmanager_verified(backend, gnw)
            live = inspect_target_mode(backend)
            if not live["stubAlive"]:
                raise TargetModeError(
                    "gnwmanager did not reach live IDLE after startup "
                    f"(VTOR 0x{live['vtor']:08x}, status 0x{live['status']:08x})"
                )
            mark_backend_flasher_ready(backend)
            state = {
                "mode": "gnwmanager-recovery",
                "stubResident": live["stubResident"],
                "stubAlive": True,
                "stubDetected": True,
                "serviceState": "idle",
                "vtor": live["vtor"],
                "dhcsr": live["dhcsr"],
                "status": live["status"],
                "halted": live["halted"],
            }
            result.update(
                state,
                application="gnwmanager Recovery stub",
                detail="Locked device; RAM flasher started and verified live",
            )
            result["installed"] = (
                f"Stock {stock_model} (locked; bank scan unavailable)"
                if stock_model
                else "Locked: firmware scan unavailable"
            )
            return result
        except Exception as exc:
            result.update(
                application="Unknown: recovery start failed",
                detail=f"Locked device; could not start recovery flasher: {exc}",
            )
            result["installed"] = (
                f"Stock {stock_model} (locked; bank scan unavailable)"
                if stock_model
                else "Locked: firmware scan unavailable"
            )
            return result
    if locked is True:
        result["installed"] = "Locked: firmware scan unavailable"
    elif locked is not False:
        result["installed"] = "Lock state unknown: firmware scan unavailable"
    elif cached_banks is not None:
        result.update(
            banks=cached_banks,
            installed=(
                "; ".join(item["image"] for item in cached_banks if item["image"] != "Erased")
                or "No firmware detected"
            ),
        )
    else:
        try:
            banks, installed = identify_installed_firmware(backend)
            result.update(banks=banks, installed=installed)
        except Exception as exc:
            result["installedDetail"] = f"Internal-flash scan failed: {exc}"

    # Keep the builder's service-liveness signal separate from the runtime
    # name: an unlocked flash VTOR remains the authoritative application view.
    if state["serviceState"] == "idle" and not unlocked_flash_runtime:
        result.update(
            application="gnwmanager Recovery stub",
            detail="gnwmanager mailbox is IDLE and the core is running",
        )
        return result
    if state["serviceState"] == "idle":
        result["detail"] = (
            "Unlocked firmware runtime follows VTOR; the gnwmanager mailbox "
            "also reports a live idle service"
        )

    if RAM_STUB_START <= vtor < RAM_STUB_END:
        status = state["status"]
        mailbox = f"0x{status:08x}" if status is not None else "unreadable mailbox"
        core = (
            "halted"
            if state["halted"]
            else "running" if state["halted"] is not None else "core state unknown"
        )
        if isinstance(status, int) and status & 0xFFFF0000 == 0xBAD00000:
            error_name = GNWMANAGER_STATUS_NAMES.get(status, mailbox)
            result.update(
                application=f"gnwmanager Recovery stub: {error_name}",
                detail=f"RAM flasher reported {error_name}; {core}",
            )
            return result
        application = (
            "Unverified SRAM runtime"
            if status is None or state["halted"] is None
            else "Transient RAM application"
        )
        result.update(
            application=application,
            detail=(
                f"VTOR is in SRAM; gnwmanager Recovery stub is not verified live "
                f"({mailbox}, {core})"
            ),
        )
        return result

    if VTOR_BANK1_START <= vtor < VTOR_BANK2_START:
        bank1 = next((item for item in result["banks"] if item["bank"] == 1), {})
        installed_image = bank1.get("image", "")
        model = stock_model
        # Match web-builder's runtime path: a bank scan that identifies OFW is
        # authoritative, and its quick Retro-Go version probe is skipped for
        # OFW vectors. In particular, do not interpret a stray GIT_TAG string
        # in the unused/bootloader portion of stock bank 1 as the running app.
        bank1_is_ofw = " OFW (" in installed_image
        version = (
            _retrogo_version(backend, VTOR_BANK1_START)
            if locked is False and not bank1_is_ofw
            else None
        )
        # gnw-web-builder classifies the bank-1 bootloader vector range before
        # interpreting the residual ITCM stock fingerprint.
        if BOOTLOADER_START <= vtor < BOOTLOADER_END:
            result.update(
                application="Bootloader", detail="VTOR is in the bank 1 bootloader region"
            )
        elif installed_image.startswith("Retro-Go "):
            image = f"Retro-Go {version}" if version else installed_image
            _refresh_bank_image(result, 1, image)
            result.update(
                application=image,
                detail=(
                    "VTOR is in bank 1; its Retro-Go version marker was read live"
                    if version
                    else "VTOR is in bank 1; bank inventory identifies a Retro-Go image"
                ),
            )
        elif installed_image.startswith("Sylverb bootloader (standalone)"):
            result.update(
                application="Bootloader",
                detail="Installed bank 1 matches the standalone bootloader",
            )
        elif " OFW (" in installed_image:
            installed_model = installed_image.split(" OFW (", 1)[0]
            protection = (
                "locked"
                if locked is True
                else "unlocked" if locked is False else "lock state unknown"
            )
            result.update(
                application=f"Stock {installed_model} ({protection})",
                detail="VTOR is in bank 1; installed bank scan found stock firmware",
            )
        elif version:
            image = f"Retro-Go {version}"
            _refresh_bank_image(result, 1, image)
            result.update(
                application=image,
                detail="VTOR is in bank 1; its Retro-Go version marker was read live",
            )
        elif model:
            protection = (
                "locked"
                if locked is True
                else "unlocked" if locked is False else "lock state unknown"
            )
            result.update(
                application=f"Stock {model} ({protection})",
                detail=(
                    "VTOR is in bank 1; residual ITCM matches the stock firmware "
                    f"fingerprint; device is {protection}"
                ),
            )
        else:
            version = _retrogo_version(backend, VTOR_BANK1_START)
            if version:
                result.update(
                    application=f"Retro-Go {version}",
                    detail="VTOR is in bank 1; Retro-Go version marker found",
                )
        if result["application"] != "Unknown":
            return result

    if VTOR_BANK2_START <= vtor < VTOR_BANK_END:
        version = _retrogo_version(backend, VTOR_BANK2_START) if locked is False else None
        image = f"Retro-Go {version}" if version else "Retro-Go"
        _refresh_bank_image(result, 2, image)
        # On this hardware bank 2 is the Retro-Go runtime region. The builder
        # reports Retro-Go from VTOR even when the optional version-string scan
        # misses (older or customized builds can omit the expected GIT_TAG).
        detail = (
            "VTOR is in bank 2; its Retro-Go version marker was read live"
            if version
            else "VTOR is in bank 2; Retro-Go runtime identified by its vector table"
        )
        result.update(application=image, detail=detail)
        return result

    # Like web-builder, PC sampling is an explicit startup diagnostic only.
    # The daemon's five-second heartbeat stays VTOR-only and never halts firmware.
    if locked is False and pc_fallback:
        try:
            pc, resumed = _read_pc_snapshot(backend, halted)
            pc_runtime = _runtime_from_pc(pc, result["banks"], stock_model, locked)
            if pc_runtime:
                result.update(
                    application=pc_runtime[0],
                    detail=f"VTOR 0x{vtor:08x} was unclassified; {pc_runtime[1]} "
                    f"(PC 0x{pc:08x})",
                    pc=pc,
                    halted=resumed,
                )
                return result
        except Exception as exc:
            result["runtimeDetail"] = f"PC fallback was unavailable: {exc}"
    result.update(detail=f"VTOR 0x{vtor:08x} is outside known firmware regions")
    return result


def _read_pc_snapshot(backend, was_halted: bool | None) -> tuple[int, bool | None]:
    """Read PC, briefly halting a running target and restoring its prior state."""
    halted_by_us = False
    if was_halted is not True:
        backend.halt()
        dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
        if not dhcsr & S_HALT:
            raise RuntimeError("target did not enter halted state")
        halted_by_us = True
    try:
        pc = int(backend.read_register("pc")) & 0xFFFFFFFF
    finally:
        if halted_by_us:
            backend.resume()
    try:
        final_halted = bool(int(backend.read_uint32(DHCSR)) & S_HALT)
    except Exception:
        final_halted = None
    if halted_by_us and final_halted is not False:
        raise RuntimeError("target did not resume after the PC snapshot")
    return pc, final_halted


def _runtime_from_pc(
    pc: int, banks: list[dict], stock_model: str | None, locked: bool | None
) -> tuple[str, str] | None:
    address = pc & ~1
    if RAM_STUB_START <= address < RAM_STUB_END:
        return "Transient RAM application", "PC points into SRAM"
    if BOOTLOADER_START <= address < BOOTLOADER_END:
        return "Bootloader", "PC points into the bank 1 bootloader region"
    if VTOR_BANK2_START <= address < VTOR_BANK_END:
        row = next((item for item in banks if item["bank"] == 2), {})
        image = row.get("image", "")
        return (
            image if image.startswith("Retro-Go ") else "Retro-Go",
            "PC points into the bank 2 Retro-Go region",
        )
    if VTOR_BANK1_START <= address < VTOR_BANK2_START:
        row = next((item for item in banks if item["bank"] == 1), {})
        image = row.get("image", "")
        if stock_model:
            variant = (
                "locked"
                if locked is True
                else "unlocked" if locked is False else "lock state unknown"
            )
            return (
                f"Stock {stock_model} ({variant})",
                "PC points into bank 1; ITCM matches stock firmware",
            )
        if image.startswith("Retro-Go "):
            return image, "PC points into the installed Retro-Go image in bank 1"
        if image.startswith("Sylverb bootloader (standalone)"):
            return "Bootloader", "PC points into the installed standalone bootloader"
        if " OFW (" in image:
            model = image.split(" OFW", 1)[0]
            variant = (
                "locked"
                if locked is True
                else "unlocked" if locked is False else "lock state unknown"
            )
            return f"Stock {model} ({variant})", "PC points into installed stock firmware in bank 1"
    return None


def _retrogo_version(backend, bank_base: int) -> str | None:
    """Read web-builder's compact 32 KiB GIT_TAG window for the active bank."""
    try:
        window = backend.read_memory(bank_base + RETROGO_VERSION_WINDOW, 32 << 10)
        match = re.search(rb"Retro-Go (SD )?(v\d[\w.+-]*|NOTAG)", window)
    except Exception:
        return None
    return (
        (("SD " if match.group(1) else "") + match.group(2).decode("ascii", errors="replace"))
        if match
        else None
    )


def _classify_gnwmanager_state(
    *, status: int | None, halted: bool | None, mailbox_alive: bool
) -> str | None:
    """Mirror web-builder's mailbox liveness and expose active service work."""
    if mailbox_alive:
        return "idle"
    if status in GNWMANAGER_ACTIVE_STATUSES:
        return "stalled" if halted is True else "busy"
    return None


def _refresh_bank_image(result: dict, index: int, image: str) -> None:
    """Refresh a running Retro-Go bank's version without waiting for a full scan."""
    for bank in result.get("banks", []):
        if bank.get("bank") == index:
            bank["image"] = image
            break
    parts = [
        bank.get("image", "") for bank in result.get("banks", []) if bank.get("image") != "Erased"
    ]
    result["installed"] = "; ".join(parts) if parts else "No firmware detected"


def _stock_itcm_model(backend) -> str | None:
    """Detect the stock Mario/Zelda ITCM fingerprint without reading flash banks."""
    for name, (offset, expected) in STOCK_ITCM_SIGNATURES.items():
        try:
            if hashlib.sha1(backend.read_memory(offset, 1300)).hexdigest() == expected:
                return name
        except Exception:
            return None
    return None


def identify_installed_firmware(backend) -> tuple[list[dict], str]:
    """Mirror web-builder's bank-vector, OFW, Retro-Go, and bootloader probes.

    Reads are passive. The routine never halts or resets the core. Internal flash is
    classified independently from VTOR so running Retro-Go can coexist with stock OFW
    in bank 1 in the reported installed layout.
    """
    banks: list[dict] = []
    for index, base in ((1, VTOR_BANK1_START), (2, VTOR_BANK2_START)):
        head = backend.read_memory(base, 8)
        if len(head) != 8:
            raise RuntimeError(f"short read from bank {index} vector table")
        sp = int.from_bytes(head[:4], "little")
        pc = int.from_bytes(head[4:], "little")
        model = OFW_INITIAL_SP.get(sp)
        # Bank 2 is the Retro-Go slot. Check its compact GIT_TAG window before
        # trusting a stack-pointer collision with the Mario/Zelda OFW vectors.
        # This lets us identify the installed image even when its initial SP
        # happens to equal an OFW model signature.
        version = _retrogo_version(backend, base) if index == 2 else None
        if version and _valid_app_vectors(sp, pc):
            banks.append(
                {
                    "bank": index,
                    "image": f"Retro-Go {version}",
                    "size": _installed_bank_size(backend, base),
                }
            )
            continue
        if model:
            marker = backend.read_memory(base + OFW_IMAGE_SIZE - 1, 1)
            patched = len(marker) == 1 and marker[0] != 0xFF
            label = f"{model} OFW ({'patched' if patched else 'stock'})"
            if index == 1 and _bootloader_match(backend, base + 0x32000, DUAL_BOOTLOADER_HASHES):
                label += " + Sylverb bootloader"
            banks.append({"bank": index, "image": label, "size": OFW_IMAGE_SIZE})
            continue
        if sp == 0xFFFFFFFF and pc == 0xFFFFFFFF:
            # A vector-only erased check is insufficient: require the same high-bank
            # occupancy probes used by web-builder before calling a bank empty.
            used_size = _installed_bank_size(backend, base)
            if used_size == 0:
                banks.append({"bank": index, "image": "Erased", "size": 0})
                continue
        else:
            used_size = _installed_bank_size(backend, base)
        version = version or _retrogo_version(backend, base)
        has_retrogo = version is not None
        if not has_retrogo and used_size:
            image = backend.read_memory(base, min(INTERNAL_BANK_SIZE, (used_size + 3) & ~3))
            has_retrogo = b"Retro-Go" in image
            if has_retrogo:
                match = re.search(rb"Retro-Go (SD )?(v\d[\w.+-]*|NOTAG)", image)
                version = (
                    (
                        ("SD " if match.group(1) else "")
                        + match.group(2).decode("ascii", errors="replace")
                    )
                    if match
                    else None
                )
        if has_retrogo and _valid_app_vectors(sp, pc):
            label = f"Retro-Go {version}" if version else "Retro-Go (older release)"
            banks.append({"bank": index, "image": label, "size": used_size})
            continue
        if used_size == 0:
            banks.append({"bank": index, "image": "Erased", "size": 0})
            continue
        label = "Unknown firmware"
        if index == 1:
            # Web-builder verifies release hashes at the standalone vector and
            # dual-bootloader offsets before calling bank 1 a bootloader.
            if (
                _valid_app_vectors(sp, pc)
                and (pc & ~1) < base + 0x32000
                and _bootloader_match(backend, base, STANDALONE_BOOTLOADER_HASHES)
            ):
                label = "Sylverb bootloader (standalone)"
            elif _bootloader_match(backend, base + 0x32000, DUAL_BOOTLOADER_HASHES):
                label = "Sylverb bootloader (dual-boot)"
        banks.append({"bank": index, "image": label, "size": used_size})

    installed_parts = [item["image"] for item in banks if item["image"] != "Erased"]
    installed = "; ".join(installed_parts) if installed_parts else "No firmware detected"
    return banks, installed


def _bootloader_match(backend, address: int, signatures: set[tuple[int, str]]) -> bool:
    maximum = max((size for size, _ in signatures), default=0)
    candidate = backend.read_memory(address, maximum)
    return any(
        len(candidate) >= size and hashlib.sha256(candidate[:size]).hexdigest() == digest
        for size, digest in signatures
    )


def _valid_app_vectors(sp: int, pc: int) -> bool:
    sp_ok = (sp >> 24) in (0x20, 0x24, 0x30, 0x38) and sp not in (0, 0xFFFFFFFF)
    pc_ok = (pc >> 24) == 0x08 and pc & 1 and VTOR_BANK1_START <= (pc & ~1) < VTOR_BANK_END
    return sp_ok and pc_ok


def _installed_bank_size(backend, base: int) -> int:
    """Use web-builder's 4 KiB probes at 32 KiB intervals to bound bank reads."""
    stride, probe = 32 << 10, 4 << 10
    rough_top = 0
    for top in range(INTERNAL_BANK_SIZE, 0, -stride):
        lower = max(0, top - probe)
        try:
            data = backend.read_memory(base + lower, min(probe, top))
        except Exception:
            raise
        if len(data) != min(probe, top):
            raise RuntimeError(f"short occupancy read from bank at 0x{base + lower:08x}")
        if any(byte != 0xFF for byte in data):
            rough_top = top
            break
    if not rough_top:
        # The descending 32 KiB stride can end above offset zero. Probe the
        # bank base explicitly so a short image (or a vector-only partial write)
        # is not reported as erased just because all high probes were blank.
        try:
            base_window = backend.read_memory(base, probe)
        except Exception:
            return 0
        if len(base_window) != probe:
            raise RuntimeError(f"short occupancy read from bank at 0x{base:08x}")
        if not any(byte != 0xFF for byte in base_window):
            return 0
        rough_top = probe
    search_base = rough_top
    search_ceil = min(INTERNAL_BANK_SIZE, rough_top + stride - probe)
    step = 16 << 10
    while step >= 8 << 10:
        if search_base + step <= search_ceil:
            lower = search_base + step - probe
            try:
                data = backend.read_memory(base + lower, probe)
            except Exception:
                break
            if any(byte != 0xFF for byte in data):
                search_base += step
        step >>= 1
    return search_base


def assert_application_mode(backend, *, operation: str) -> dict:
    """Refuse to overwrite a resident RAM programmer from an unrelated run.

    The check must happen after attaching and before calling
    ``GnW.start_gnwmanager()``, which resets the device and overwrites the
    running RAM image.
    """
    state = inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    if state["stubDetected"]:
        mark_backend_recovery_required(backend, "gnwmanager RAM programmer")
        state_label = (
            "live and idle"
            if state["stubAlive"]
            else f"resident but not confirmed idle (status 0x{state['status']:08x}, "
            f"{'halted' if state['halted'] else 'running'})"
        )
        raise TargetModeError(
            f"Cannot {operation}: the gnwmanager RAM flasher is {state_label} "
            f"(VTOR 0x{state['vtor']:08x}). This operation requires application "
            "firmware, so the flasher was left untouched. Finish deployment or "
            "start a firmware bank before retrying."
        )
    return state


def start_gnwmanager_verified(backend, gnw, *, retries: int = 3) -> None:
    """Load and verify the RAM flasher before allowing it to execute.

    gnwmanager's stock ``start_gnwmanager()`` trusts the debug-probe RAM write
    and register writes. gnw-web-builder reads the complete blob back before
    resume; mirror that safeguard so a short/corrupt SWD transfer cannot leave
    the target running a damaged stub.
    """
    from importlib.resources import files
    from gnwmanager.time import timestamp_now

    watchdog_freezes = _reset_and_halt_for_stub(backend, gnw)
    dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
    if not dhcsr & S_HALT:
        # A reset may leave a core in LOCKUP: target state can be non-running
        # without DHCSR.S_HALT being set.
        # Do not issue RAM writes or access core registers until verified.
        try:
            backend.halt()
            dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
        except Exception as error:
            raise TargetModeError(
                "Reset did not leave the core halted; GWProv could not confirm an "
                f"explicit halt ({error}). No flasher image or registers were written."
            ) from error
        if not dhcsr & S_HALT:
            raise TargetModeError(
                "Reset did not leave the core halted, and an explicit halt was not "
                "confirmed (DHCSR.S_HALT is clear). No flasher image or registers "
                "were written."
            )
    firmware = (files("gnwmanager") / "firmware.bin").read_bytes()
    if len(firmware) < 8:
        raise TargetModeError("gnwmanager RAM flasher image is too short to contain its vectors")

    last_error = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            backend.write_memory(GNWMANAGER_FIRMWARE_ADDR, firmware)
            actual = backend.read_memory(GNWMANAGER_FIRMWARE_ADDR, len(firmware))
            if actual == firmware:
                break
            mismatch = next(
                (
                    index
                    for index, (expected, found) in enumerate(zip(firmware, actual))
                    if expected != found
                ),
                min(len(firmware), len(actual)),
            )
            last_error = (
                f"read-back mismatch at image offset 0x{mismatch:x} "
                f"(expected {len(firmware)} bytes, read {len(actual)})"
            )
        except Exception as error:
            last_error = str(error)
        if attempt == max(1, retries):
            raise TargetModeError(
                "gnwmanager RAM flasher failed read-back verification after "
                f"{attempt} attempt(s): {last_error}. The core remains halted; "
                "the damaged image was not started."
            )

    gnw.write_uint32("status", 0)
    gnw.write_uint32("status_override", 0)
    msp = int.from_bytes(firmware[:4], "little")
    pc = int.from_bytes(firmware[4:8], "little")
    backend.write_register("msp", msp)
    backend.write_register("pc", pc)
    actual_msp = backend.read_register("msp") & 0xFFFFFFFF
    actual_pc = backend.read_register("pc") & 0xFFFFFFFF
    if actual_msp != msp or (actual_pc & ~1) != (pc & ~1):
        raise TargetModeError(
            "gnwmanager RAM flasher vector registers did not read back correctly "
            f"(MSP expected 0x{msp:08x}, read 0x{actual_msp:08x}; "
            f"PC expected 0x{pc:08x}, read 0x{actual_pc:08x}). "
            "The core remains halted; the image was not started."
        )

    _resume_after_stub_transition(backend, watchdog_freezes)
    gnw.wait_for_idle()
    gnw.write_uint32("utc_timestamp", timestamp_now())
    gnw._gnwmanager_started = True


def _reset_and_halt_for_stub(backend, gnw) -> dict[int, tuple[int, int]]:
    """Freeze watchdogs, then use the backend's reset-catch sequence."""
    changed = _freeze_h7_watchdogs(backend)
    # PyOCD's reset_and_halt implements the builder's vector-catch + SYSRESETREQ
    # sequence and also performs DP post-reset recovery, which must not be
    # bypassed by writing AIRCR directly through the generic backend.
    gnw.reset_and_halt()
    dhcsr = int(backend.read_uint32(DHCSR)) & 0xFFFFFFFF
    if not dhcsr & S_HALT:
        # Keep watchdogs frozen if reset did not leave the core safely halted.
        raise TargetModeError(
            "Core did not halt after reset; watchdogs remain frozen and no RAM image was written"
        )
    return changed


def _resume_after_stub_transition(backend, watchdog_freezes) -> None:
    backend.resume()
    _restore_h7_watchdogs(backend, watchdog_freezes)


def _freeze_h7_watchdogs(backend) -> dict[int, tuple[int, int]]:
    """Freeze H7 watchdog counters before halting; return only bits we changed."""
    changed: dict[int, tuple[int, int]] = {}
    for address, mask in ((DBGMCU_APB3FZ1, DBG_WWDG1), (DBGMCU_APB4FZ1, DBG_IWDG1)):
        try:
            value = int(backend.read_uint32(address)) & 0xFFFFFFFF
            if value == 0xFFFFFFFF or value & mask:
                continue
            backend.write_uint32(address, value | mask)
            changed[address] = (value, mask)
        except Exception:
            # Match the builder: watchdog freeze is best-effort, but still
            # attempt the halt so a failed debug-register read is actionable.
            continue
    return changed


def _restore_h7_watchdogs(backend, changed: dict[int, tuple[int, int]]) -> None:
    """Undo only watchdog freeze bits GWProv set, after the core is running."""
    for address, (before, mask) in changed.items():
        try:
            current = int(backend.read_uint32(address)) & 0xFFFFFFFF
            if current != 0xFFFFFFFF and not before & mask and current & mask:
                backend.write_uint32(address, current & ~mask)
        except Exception:
            continue


def mark_backend_recovery_required(backend, phase: str) -> None:
    """Deprecated compatibility hook; recovery state is read from the target."""
    return None


def mark_backend_flasher_ready(backend) -> None:
    """Deprecated compatibility hook; recovery state is read from the target."""
    return None


def clear_backend_recovery_required(backend) -> None:
    """Deprecated compatibility hook; no host-side recovery marker is kept."""
    return None


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
        if (
            status_before == STATUS_IDLE
            and ready_before == [0, 0]
            and upload_before == 0
            and download_before == 0
        ):
            counter = backend.read_uint32(GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            status_after = backend.read_uint32(MAILBOX_STATUS) & 0xFFFFFFFF
            ready_after = [backend.read_uint32(addr) & 0xFFFFFFFF for addr in ready_addresses]
            upload_after = gnw.read_uint32("upload_in_progress") & 0xFFFFFFFF
            download_after = gnw.read_uint32("download_in_progress") & 0xFFFFFFFF
            counter_after = backend.read_uint32(GNWMANAGER_CONTEXT_COUNTER) & 0xFFFFFFFF
            if (
                status_after == STATUS_IDLE
                and ready_after == [0, 0]
                and upload_after == 0
                and download_after == 0
                and counter != 0
                and counter_after == counter
            ):
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

    state = inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    if state["stubAlive"]:
        mark_backend_recovery_required(backend, "synchronizing gnwmanager RAM flasher")
        counter = attach_to_idle_gnwmanager(backend, gnw)
        state = inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
        if not state["stubAlive"]:
            raise TargetModeError(
                "Recovery could not confirm the RAM flasher remained live and idle "
                f"(VTOR 0x{state['vtor']:08x}, status 0x{state['status']:08x})."
            )
        mark_backend_flasher_ready(backend)
        return {**state, "contextCounter": counter, "started": False}
    terminal_error = (state["status"] & 0xFFFF0000) == 0xBAD00000
    recoverable_resident_stub = (
        state["stubResident"] and state["status"] == STATUS_IDLE and state["halted"]
    )
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
    start_gnwmanager_verified(backend, gnw)
    state = inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    if not state["stubAlive"]:
        raise TargetModeError(
            "Recovery failed: gnwmanager did not reach live IDLE after startup "
            f"(VTOR 0x{state['vtor']:08x}, status 0x{state['status']:08x})."
        )
    counter = attach_to_idle_gnwmanager(backend, gnw)
    state = inspect_target_mode(backend, verify_flash_vtor_mailbox=True)
    if not state["stubAlive"]:
        raise TargetModeError(
            "Recovery could not confirm the RAM flasher remained live and idle "
            f"(VTOR 0x{state['vtor']:08x}, status 0x{state['status']:08x})."
        )
    mark_backend_flasher_ready(backend)
    return {**state, "contextCounter": counter, "started": True}
