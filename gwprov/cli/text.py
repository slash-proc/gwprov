"""Rich-based renderers for human-facing command output."""
from __future__ import annotations

from pathlib import Path
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
        if row.get("gdbPort"):
            parts.append(f"GDB {row['gdbPort']}")
        if row.get("qmpSocket"):
            parts.append("daemon QMP" if str(row["qmpSocket"]).startswith("gwprov://") else "QMP")
        return "\n".join(parts)
    parts = [str(value) for value in (row.get("vendor"), row.get("backend")) if value]
    if row.get("probeId"):
        parts.append(f"SN {row['probeId']}")
    return "\n".join(parts) or "—"


def _short_device(row: dict[str, Any]) -> str:
    if row.get("kind") == "gwemu" or row.get("pid") is not None:
        return "GWemu"
    runtime = row.get("runtime") if isinstance(row.get("runtime"), dict) else {}
    value = str(row.get("device") or row.get("target") or runtime.get("device")
                or runtime.get("target") or row.get("name") or "Unknown")
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
                variant = image[len(prefix) + 2:-1].casefold()
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
        "Bootloader" if any(bank == "bank1" for bank, _ in bootloaders) else "Erased")
    bank2 = ", ".join(labels["bank2"]) or (
        "Bootloader" if any(bank == "bank2" for bank, _ in bootloaders) else "Erased")
    return boot, bank1, bank2


def _firmware_columns(row: dict[str, Any]) -> tuple[str, str, str]:
    runtime = row.get("runtime")
    if not isinstance(runtime, dict):
        return _profile_firmware(row)
    banks = {int(item.get("bank", 0)): str(item.get("image") or "Unknown")
             for item in runtime.get("banks", []) if isinstance(item, dict)}
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
        return "Mario (patched)" if "patched" in bank1.casefold() else "Mario (stock)"
    if lower.startswith("stock zelda"):
        return "Zelda (patched)" if "patched" in bank1.casefold() else "Zelda (stock)"
    if lower == "bootloader":
        return "Bootloader"
    if lower.startswith("retro-go"):
        version = app[len("Retro-Go"):].strip()
        if " sd " in f" {version.casefold()} ":
            return f"Retro-Go {version}".strip()
        if version.startswith("v"):
            return f"Retro-Go SD {version}"
        return f"Retro-Go {version}".strip()
    profile_version = next((label for label in (bank1, bank2)
                            if label.casefold().startswith("retro-go")), None)
    if profile_version and row.get("kind") == "gwemu":
        if app.casefold() not in {"running", "initializing", "unknown"}:
            return f"{profile_version} [{app}]"
        return profile_version
    return app


def _details(row: dict[str, Any]) -> str:
    info = (row.get("stateDetail") or row.get("applicationDetail")
            or row.get("installedDetail") or row.get("detail"))
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
            kind = "GWemu" if row.get("kind") == "gwemu" or row.get("pid") is not None else "Hardware"
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
        kind = "GWemu" if row.get("kind") == "gwemu" or row.get("pid") is not None else "Hardware"
        kind_text = Text(kind + "\n")
        state_text = _status(row.get("status"))
        kind_text.append(state_text)
        boot, bank1, bank2 = _firmware_columns(row)
        details = _details(row)
        table.add_row(Text(_display_name(row)), kind_text,
                      Text(_short_device(row)), Text(_connection(row)), Text(boot),
                      Text(bank1), Text(bank2), Text(_running(row, bank1, bank2)),
                      Text(details, style="dim"))

    return table


def print_process_list(rows: list[dict[str, Any]], *, title: str = "Devices",
                       no_pager: bool = False, console: Console | None = None) -> None:
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


def render_process_list(rows: list[dict[str, Any]], *, title: str = "Devices",
                        width: int = 100) -> str:
    """Return plain text for tests and logs using the same Rich layout."""
    from io import StringIO

    output = StringIO()
    console = Console(file=output, width=width, color_system=None, force_terminal=False)
    print_process_list(rows, title=title, no_pager=True, console=console)
    return output.getvalue()
