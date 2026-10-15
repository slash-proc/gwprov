"""Rich-based renderers for human-facing command output."""

from __future__ import annotations

import json
from pathlib import Path
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from typing import Any

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

_STATE_STYLES = {
    "running": "bold green",
    "ready": "bold green",
    "halted": "bold yellow",
    "recovery": "bold yellow",
    "busy": "bold cyan",
    "unknown": "bold red",
}


def _display_name(row: dict[str, Any]) -> str:
    if row.get("displayName"):
        return str(row["displayName"])
    if (row.get("kind") == "gwemu" or row.get("pid") is not None) and row.get("profile"):
        base = Path(str(row["profile"])).expanduser().name or "gwemu"
        return f"{base}-01"
    return str(row.get("name") or "Unnamed device")


def _connection(row: dict[str, Any]) -> str:
    if row.get("pid") is not None:
        parts = [f"PID {row['pid']}"]
        if "display" in row:
            parts.append(f"Display {'on' if row['display'] else 'off'}")
        if row.get("gdbEndpoint"):
            endpoint = row["gdbEndpoint"]
            label = "GDB candidate" if row.get("gdbListener") is False else "GDB"
            parts.append(f"{label} {endpoint}")
        elif row.get("gdbPort"):
            parts.append(f"GDB {row['gdbPort']}")
        if row.get("qmpSocket"):
            if str(row["qmpSocket"]).startswith("gwprov://"):
                parts.append("QMP daemon")
            else:
                parts.append(f"QMP {row.get('qmpTransport') or 'socket'}")
        listeners = row.get("tcpListeners")
        if not row.get("gdbEndpoint") and listeners:
            parts.append("TCP listeners " + ", ".join(listeners))
        return "\n".join(parts)
    parts = [str(value) for value in (row.get("vendor"), row.get("backend")) if value]
    if row.get("probeId"):
        parts.append(f"SN {row['probeId']}")
    return "\n".join(parts) or "—"


def _short_device(row: dict[str, Any]) -> str:
    if row.get("kind") == "gwemu" or row.get("pid") is not None:
        return "GWemu"
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    value = str(
        row.get("device")
        or row.get("target")
        or runtime.get("device")
        or runtime.get("target")
        or row.get("name")
        or "Unknown"
    )
    folded = value.casefold()
    if folded == "stm32 stlink":
        return "H7B0"
    if folded.startswith("stm32"):
        value = value[5:]
    if value.casefold().endswith("xx"):
        value = value[:-2]
    return value


def _firmware_label(value: Any) -> str:
    image = str(value or "Unknown")
    image = image.replace(" + Sylverb bootloader", "")
    for model in ("Mario", "Zelda"):
        for prefix in (f"Stock {model}", f"{model} OFW"):
            if image == prefix:
                return f"{model} (stock)"
            if image.startswith(prefix + " (") and image.endswith(")"):
                variant = image[len(prefix) + 2 : -1].casefold()
                if "+patch" in variant or "patched" in variant:
                    return f"{model} (patched)"
                if "stock" in variant:
                    return f"{model} (stock)"
        prefix = f"{model} OFW ("
        if image.startswith(prefix) and image.endswith(")"):
            return f"{model} ({image[len(prefix):-1]})"
        prefix = f"Stock {model} ("
        if image.startswith(prefix) and image.endswith(")"):
            return f"{model} ({image[len(prefix):-1]})"
    if image.startswith("Sylverb bootloader (standalone)"):
        return "Bootloader"
    return image


def _profile_firmware(row: dict[str, Any]) -> tuple[str, str, str]:
    """Return bootloader location and profile bank labels without target I/O."""
    profile = row.get("profile") if row.get("kind") == "gwemu" else row.get("assignedProfile")
    if not profile:
        return "Unknown", "Unknown", "Unknown"
    map_path = Path(str(profile)).expanduser() / "flash-map.yaml"
    try:
        import yaml
    except ImportError:
        return "Unknown", "Unknown", "Unknown"
    try:
        mapping = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        return "Unknown", "Unknown", "Unknown"
    if not isinstance(mapping, dict):
        return "Unknown", "Unknown", "Unknown"
    labels: dict[str, list[str]] = {"bank1": [], "bank2": []}
    bootloaders = []
    for bank in ("bank1", "bank2"):
        entries = mapping.get(bank, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "Unknown")
            if "bootloader" in name.casefold():
                try:
                    offset = int(entry.get("offset", 0) or 0)
                except (TypeError, ValueError):
                    offset = 0
                bootloaders.append((bank, offset))
            else:
                labels[bank].append(name)
    boot = "None"
    if bootloaders:
        bank, offset = bootloaders[0]
        kind = "dual-boot" if offset else "standalone"
        boot = f"Bank {bank[-1]} ({kind})"
    bank1 = ", ".join(labels["bank1"]) or (
        "Bootloader" if any(bank == "bank1" for bank, _ in bootloaders) else "Erased"
    )
    bank2 = ", ".join(labels["bank2"]) or (
        "Bootloader" if any(bank == "bank2" for bank, _ in bootloaders) else "Erased"
    )
    return boot, bank1, bank2


def _firmware_columns(row: dict[str, Any]) -> tuple[str, str, str]:
    runtime = row.get("runtime")
    if row.get("kind") == "remote-gdb" and not isinstance(runtime, dict):
        return "Unknown", "Unknown", "Unknown"
    if not isinstance(runtime, dict):
        return _profile_firmware(row)
    banks = {
        int(item.get("bank", 0)): str(item.get("image") or "Unknown")
        for item in runtime.get("banks", [])
        if isinstance(item, dict)
    }
    raw1, raw2 = banks.get(1, "Unknown"), banks.get(2, "Unknown")
    boot = "Unknown"
    if "standalone" in raw1.casefold():
        boot = "Bank 1 (standalone)"
        raw1 = "Bootloader"
    elif "dual-boot" in raw1.casefold() or "+ Sylverb bootloader" in raw1:
        boot = "Bank 1 (dual-boot)"
    elif raw1 not in {"Unknown", "Unknown firmware"}:
        boot = "None"
    return boot, _firmware_label(raw1), _firmware_label(raw2)


def _running(row: dict[str, Any], bank1: str, bank2: str) -> str:
    runtime = row.get("runtime")
    app = str((runtime or {}).get("application") or row.get("application") or "Unknown")
    if app.casefold().startswith("running: "):
        app = app[9:].strip()
    lower = app.casefold()
    if lower.startswith("stock mario"):
        variant = _stock_variant(bank1)
        return f"Mario ({variant})" if variant else "Mario (patch status unknown)"
    if lower.startswith("stock zelda"):
        variant = _stock_variant(bank1)
        return f"Zelda ({variant})" if variant else "Zelda (patch status unknown)"
    if lower == "bootloader":
        return "Bootloader"
    if lower.startswith("retro-go"):
        version = app[len("Retro-Go") :].strip()
        if " sd " in f" {version.casefold()} ":
            return f"Retro-Go {version}".strip()
        if version.startswith("v"):
            return f"Retro-Go SD {version}"
        return f"Retro-Go {version}".strip()
    profile_version = next(
        (label for label in (bank1, bank2) if label.casefold().startswith("retro-go")), None
    )
    if profile_version and row.get("kind") == "gwemu":
        if app.casefold() not in {"running", "initializing", "unknown"}:
            return f"{profile_version} [{app}]"
        return profile_version
    return app


def _stock_variant(bank: str) -> str | None:
    value = bank.casefold()
    if "patch" in value:
        return "patched"
    if "stock" in value or "ofw" in value:
        return "stock"
    return None


def _device_state(row: dict[str, Any]) -> str:
    status = str(row.get("status") or "unknown").casefold()
    if status == "busy":
        return "Busy"
    if status == "running" or row.get("running") is True:
        return "Running"
    qmp_status = str(row.get("qmpStatus") or "").casefold()
    if row.get("kind") == "gwemu":
        if qmp_status in {"debug", "prelaunch", "internal-error", "io-error"}:
            return "Halted"
        if qmp_status in {"paused", "suspended"}:
            return "Paused"
        if row.get("running") is False:
            return "Paused"
    if status == "halted":
        return "Halted"
    return "Unknown"


def _state_read(row: dict[str, Any]) -> str:
    last_observed = row.get("lastObserved")
    observed = (
        last_observed.get("polledAt") if isinstance(last_observed, dict) else row.get("polledAt")
    )
    if not isinstance(observed, (int, float)):
        return "Not read"
    seconds = max(0, int(time.time() - observed))
    if seconds == 0:
        return "Just now"
    unit = "second" if seconds == 1 else "seconds"
    return f"{seconds} {unit} ago"


def _profile_label(row: dict[str, Any]) -> str:
    value = row.get("assignedProfile") or row.get("profile")
    if not value or str(value).casefold() == "raw image":
        return "None"
    try:
        from gwprov.profiles import profile_reference

        return profile_reference(str(value))
    except (OSError, RuntimeError, ValueError):
        return str(value)


def _sd_card_label(row: dict[str, Any]) -> str:
    value = row.get("sdCard")
    if value:
        try:
            from gwprov.sd_cards import list_cards

            for card in list_cards():
                if str(value) in {str(card.get("name")), str(card.get("path"))}:
                    return str(card.get("name") or card.get("path"))
        except (OSError, RuntimeError, ValueError):
            pass
        return str(value)

    if row.get("kind") == "gwemu":
        profile = row.get("profile")
        if not profile or str(profile).casefold() == "raw image":
            return "Unknown"
        try:
            config = tomllib.loads((Path(profile) / "profile.toml").read_text(encoding="utf-8"))
            sd = config.get("sd", {})
            mode = sd.get("mode", "none")
            if mode == "none":
                return "None"
            if mode == "bundled" and sd.get("image"):
                return str((Path(profile) / sd["image"]).resolve())
        except (OSError, ValueError, TypeError):
            pass
        return "Unknown"
    return "Unknown"


def _lock_state(row: dict[str, Any]) -> str:
    runtime = row.get("runtime")
    if isinstance(runtime, dict) and isinstance(runtime.get("locked"), bool):
        return "Locked" if runtime["locked"] else "Unlocked"
    if row.get("kind") == "gwemu":
        profile = row.get("profile")
        if profile and str(profile).casefold() != "raw image":
            root = Path(profile)
            try:
                metadata = json.loads((root / "provision.json").read_text(encoding="utf-8"))
                locked = metadata.get("locked")
                if isinstance(locked, bool):
                    return ("Locked" if locked else "Unlocked") + " (profile)"
            except (OSError, ValueError):
                pass
            try:
                rdp = (root / "rdp-state.bin").read_bytes()
                if rdp and rdp[0] in (0x55, 0xAA):
                    return ("Locked" if rdp[0] == 0x55 else "Unlocked") + " (profile)"
            except OSError:
                pass
    return "Unknown"


def _device_connection(row: dict[str, Any]) -> str:
    if row.get("kind") == "gwemu":
        return "Local process"
    backend = str(row.get("backend") or "").casefold()
    adapter = str(row.get("adapterName") or row.get("name") or "").strip()
    vendor = str(row.get("vendor") or "").strip()
    label = " ".join(part for part in (vendor, adapter) if part)
    if backend in {"pyocd", "pyocd daemon"}:
        label = f"{label} via pyOCD" if label else "pyOCD"
    elif backend and backend not in label.casefold():
        label = f"{label} via {row.get('backend')}" if label else str(row["backend"])
    return label or "Unknown"


def _device_kind(row: dict[str, Any]) -> str:
    if row.get("kind") == "gwemu" or row.get("pid") is not None:
        return "GWemu"
    if row.get("kind") == "remote-gdb":
        return "Remote GDB"
    return "Hardware"


def _extflash_image_size(row: dict[str, Any]) -> str:
    profile = row.get("profile") if row.get("kind") == "gwemu" else row.get("assignedProfile")
    if not profile or str(profile).casefold() == "raw image":
        return "Unknown"
    try:
        root = Path(profile)
        config = tomllib.loads((root / "profile.toml").read_text(encoding="utf-8"))
        relative = config.get("flash", {}).get("extflash", "extflash.bin")
        image = (root / relative).resolve()
        if root.resolve() not in image.parents or not image.is_file():
            return "Unknown"
        size_mib = image.stat().st_size / (1024 * 1024)
        shown = f"{size_mib:g} MiB" if size_mib.is_integer() else f"{size_mib:.1f} MiB"
        return shown
    except (OSError, ValueError, TypeError):
        return "Unknown"


def device_detail_data(row: dict[str, Any]) -> dict[str, Any]:
    """Build stable, decoration-free detail data for one inventory row."""
    kind = _device_kind(row)
    bootloader, bank1, bank2 = _firmware_columns(row)
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    if kind in {"Hardware", "Remote GDB"} and not runtime:
        bootloader = bank1 = bank2 = "Unknown"
    summary = {
        "device": _display_name(row),
        "id": row.get("id", "Unknown"),
        "type": kind,
        "connection": _device_connection(row),
        "state": _device_state(row),
        "profile": _profile_label(row),
        "sd_card": _sd_card_label(row),
        "state_read": _state_read(row),
    }
    firmware = {
        "bootloader": bootloader,
        "bank1": bank1,
        "bank2": bank2,
        "running": (
            "Unknown"
            if row.get("externalSession") or row.get("status") == "busy"
            else _running(row, bank1, bank2)
        ),
        "lock_state": _lock_state(row),
    }
    if kind == "GWemu":
        timing = row.get("timing") if isinstance(row.get("timing"), dict) else {}
        mode = timing.get("mode")
        accuracy = {
            "default": "Not emulated",
            "baseline": "Basic device timing",
            "experimental-m7": "Full device timing (experimental WIP)",
        }.get(mode, "Unknown")
        pacing = timing.get("pacing")
        if pacing is None and mode in {"default", "experimental-m7"}:
            pacing = "unthrottled"
        elif pacing is None and mode == "baseline":
            pacing = "realtime"
        pacing_label = {"realtime": "Realtime", "unthrottled": "No pacing"}.get(pacing, "Unknown")
        identity = row.get("binaryIdentity")
        version = identity.get("version") if isinstance(identity, dict) else None
        commit = identity.get("commit") if isinstance(identity, dict) else None
        if version and commit:
            build = f"{version} ({commit})"
        elif version:
            build = f"{version} (commit unknown)"
        else:
            build = "Unknown"
        qmp_endpoint = str(row.get("qmpSocket") or "")
        qmp_transport = str(row.get("qmpTransport") or "").casefold()
        if qmp_endpoint.startswith("gwprov://") or qmp_transport == "daemon-stdio":
            qmp = "GWProv daemon"
        elif qmp_endpoint and qmp_transport in {"unix", "socket"}:
            qmp = "Local socket"
        elif qmp_endpoint:
            qmp = f"{qmp_transport.upper() or 'QMP'} {qmp_endpoint}"
        else:
            qmp = "Unavailable"
        gdb = str(row.get("gdbEndpoint") or "")
        if not gdb and row.get("gdbPort"):
            gdb = f"127.0.0.1:{row['gdbPort']}"
        if not gdb:
            gdb = "Unavailable"
        elif row.get("gdbListener") is False:
            gdb += " (not listening)"
        display = {"visible": "Windowed", "headless": "Headless"}.get(
            str(row.get("display") or "").casefold(), "Unknown"
        )
        details = {
            "pid": row.get("pid"),
            "display": display,
            "application": str(row.get("application") or "Unknown"),
            # QMP does not expose the model's decoded NOR capacity yet. Keep
            # the profile image size separate so it cannot be mistaken for it.
            "extflash": "Unknown",
            "profile_extflash_image": _extflash_image_size(row),
            "timing_accuracy": accuracy,
            "pacing": pacing_label,
            "qmp": qmp,
            "gdb": gdb,
            "gwemu_build": build,
        }
    else:
        details = {
            "target": _short_device(row),
            "adapter": str(row.get("adapterName") or row.get("name") or "Unknown"),
            "probe_id": str(row.get("probeId") or row.get("id") or "Unknown"),
            "application": str(row.get("application") or "Unknown"),
            "detail": _details(row) or "None",
        }
    return {"summary": summary, "firmware": firmware, "details": details}


def device_detail_table(row: dict[str, Any]) -> Table:
    """Build one device's detail card for terminal or captured output."""
    data = device_detail_data(row)
    table = Table(
        title=f"Device {data['summary']['device']}",
        title_justify="left",
        box=box.ROUNDED,
        border_style="bright_black",
        padding=(0, 1),
        expand=True,
        show_header=False,
        show_lines=True,
    )
    table.add_column("FIELD", style="bold", no_wrap=True, width=15)
    table.add_column("VALUE", overflow="fold")
    for section, values in (
        ("Summary", data["summary"]),
        ("Firmware", data["firmware"]),
        ("Details", data["details"]),
    ):
        table.add_section()
        table.add_row(Text(section.upper(), style="bold bright_cyan"), "")
        for key, value in values.items():
            label = {
                "id": "ID",
                "sd_card": "SD card",
                "bank1": "Bank 1",
                "bank2": "Bank 2",
                "pid": "PID",
                "probe_id": "Probe ID",
                "qmp": "QMP",
                "gdb": "GDB",
                "gwemu_build": "GWemu build",
                "profile_extflash_image": "Extflash image",
            }.get(key, key.replace("_", " ").title())
            shown = "Unknown" if value is None else str(value)
            style = _STATE_STYLES.get(shown.casefold(), "")
            table.add_row(label, Text(shown, style=style))
    return table


def render_device_detail(row: dict[str, Any], *, width: int = 100) -> str:
    """Render one device as a readable, narrow-terminal-safe detail card."""
    from rich.console import Console

    table = device_detail_table(row)
    console = Console(width=width, force_terminal=False, color_system=None)
    with console.capture() as capture:
        console.print(table)
    return capture.get()


def print_device_detail(row: dict[str, Any], *, console: Console | None = None) -> None:
    """Print one device card with the active terminal's color and width rules."""
    console = console or Console()
    console.print(device_detail_table(row))


def _details(row: dict[str, Any]) -> str:
    info = (
        row.get("stateDetail")
        or row.get("applicationDetail")
        or row.get("installedDetail")
        or row.get("detail")
    )
    if info and str(info).startswith("Pass --profile with matching firmware"):
        return ""
    return str(info or "")


def _status(value: Any) -> Text:
    state = str(value or "unknown").lower()
    style = _STATE_STYLES.get(state, "bold magenta")
    return Text(f"● {state.upper()}", style=style, no_wrap=True)


def process_table(rows: list[dict[str, Any]], *, title: str, width: int) -> Table:
    """Build a responsive, color-aware device/process inventory table."""
    if width < 190:
        table = Table(
            title=f"[bold bright_cyan]{title} ({len(rows)})[/]",
            title_justify="left",
            box=box.ROUNDED,
            border_style="bright_black",
            header_style="bold bright_white",
            padding=(0, 1),
            expand=True,
            show_edge=True,
            show_lines=True,
        )
        table.add_column("NAME", min_width=10, max_width=24, overflow="fold")
        table.add_column("DEVICE STATE", min_width=24, overflow="fold")
        for row in rows:
            kind = _device_kind(row)
            boot, bank1, bank2 = _firmware_columns(row)
            state = Text(kind + " ")
            state.append(_status(row.get("status")))
            values = (
                ("Device", _short_device(row)),
                ("Connection", _connection(row)),
                ("Bootloader", boot),
                ("Bank1", bank1),
                ("Bank2", bank2),
                ("Running", _running(row, bank1, bank2)),
                ("Details", _details(row)),
            )
            info = Text()
            info.append_text(state)
            for label, value in values:
                if not value:
                    continue
                info.append(f"\n{label}: ", style="bold")
                info.append(value, style="dim" if label == "Details" else "")
            table.add_row(Text(_display_name(row)), info)
        return table

    table = Table(
        title=f"[bold bright_cyan]{title} ({len(rows)})[/]",
        title_justify="left",
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold bright_white",
        row_styles=("", ""),
        padding=(0, 1),
        expand=True,
        show_edge=True,
        show_lines=True,
    )
    table.add_column("NAME", min_width=10, max_width=24, overflow="fold")
    table.add_column("TYPE", min_width=8, max_width=14, overflow="fold")
    table.add_column("DEVICE", min_width=6, max_width=12, overflow="fold")
    table.add_column("CONNECTION", min_width=9, max_width=28, overflow="fold")
    table.add_column("BOOTLOADER", min_width=8, max_width=20, overflow="fold")
    table.add_column("BANK1", min_width=8, max_width=22, overflow="fold")
    table.add_column("BANK2", min_width=8, max_width=22, overflow="fold")
    table.add_column("RUNNING", min_width=12, max_width=32, overflow="fold")
    table.add_column("DETAILS", min_width=10, overflow="fold")

    for row in rows:
        kind = _device_kind(row)
        kind_text = Text(kind + "\n")
        state_text = _status(row.get("status"))
        kind_text.append(state_text)
        boot, bank1, bank2 = _firmware_columns(row)
        details = _details(row)
        table.add_row(
            Text(_display_name(row)),
            kind_text,
            Text(_short_device(row)),
            Text(_connection(row)),
            Text(boot),
            Text(bank1),
            Text(bank2),
            Text(_running(row, bank1, bank2)),
            Text(details, style="dim"),
        )

    return table


def print_process_list(
    rows: list[dict[str, Any]],
    *,
    title: str = "Devices",
    no_pager: bool = False,
    console: Console | None = None,
) -> None:
    """Print an inventory, paging long interactive output unless disabled."""
    console = console or Console()
    table = process_table(rows, title=title, width=console.width)
    if not rows:
        console.print(f"[dim]No {title.lower()} found.[/]")
        return

    # Page only when the rendered table would scroll off an interactive screen.
    # Piped output is always immediate, and --no-pager always bypasses paging.
    line_count = len(console.render_lines(table, console.options))
    should_page = console.is_terminal and not no_pager and line_count > console.height
    if should_page:
        with console.pager(styles=True):
            console.print(table)
    else:
        console.print(table)


def render_process_list(
    rows: list[dict[str, Any]], *, title: str = "Devices", width: int = 100
) -> str:
    """Return plain text for tests and logs using the same Rich layout."""
    from io import StringIO

    output = StringIO()
    console = Console(file=output, width=width, color_system=None, force_terminal=False)
    print_process_list(rows, title=title, no_pager=True, console=console)
    return output.getvalue()
