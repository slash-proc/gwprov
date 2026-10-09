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
    "halted": "bold yellow",
    "busy": "bold cyan",
    "unknown": "bold red",
}


def _display_name(row: dict[str, Any]) -> str:
    name = str(row.get("name") or "Unnamed device")
    if row.get("kind") == "gwemu" or row.get("pid") is not None:
        profile = row.get("profile")
        if profile:
            return Path(str(profile)).expanduser().name or str(profile)
    return name


def _connection(row: dict[str, Any]) -> str:
    if row.get("pid") is not None:
        parts = [f"PID {row['pid']}"]
        if "display" in row:
            parts.append(f"Display {'on' if row['display'] else 'off'}")
        if row.get("gdbPort"):
            parts.append(f"GDB :{row['gdbPort']}")
        if row.get("qmpSocket"):
            parts.append("daemon QMP" if str(row["qmpSocket"]).startswith("gwprov://") else "QMP")
        return " · ".join(parts)
    parts = [str(value) for value in (row.get("vendor"), row.get("backend")) if value]
    if row.get("probeId"):
        parts.append(str(row["probeId"]))
    return " · ".join(parts) or "—"


def _status(value: Any) -> Text:
    state = str(value or "unknown").lower()
    style = _STATE_STYLES.get(state, "bold magenta")
    return Text(f"● {state.upper()}", style=style, no_wrap=True)


def process_table(rows: list[dict[str, Any]], *, title: str, width: int) -> Table:
    """Build a responsive, color-aware device/process inventory table."""
    table = Table(
        title=f"[bold bright_cyan]{title}[/] [dim]· {len(rows)}",
        title_justify="left",
        box=box.ROUNDED,
        border_style="bright_black",
        header_style="bold bright_white",
        row_styles=("", ""),
        padding=(0, 1),
        expand=True,
        show_edge=True,
    )
    compact = width < 100
    table.add_column("STATE", min_width=11, no_wrap=True)
    if compact:
        table.add_column("DEVICE", min_width=13, overflow="fold")
        table.add_column("APPLICATION · CONNECTION", min_width=18, overflow="fold")
    else:
        table.add_column("TYPE", min_width=9, max_width=12, style="bright_blue", no_wrap=True)
        table.add_column("DEVICE", min_width=14, overflow="fold")
        table.add_column("APPLICATION", min_width=14, overflow="fold")
        table.add_column("CONNECTION / DETAILS", min_width=18, overflow="fold")

    for row in rows:
        kind = "GWemu" if row.get("kind") == "gwemu" or row.get("pid") is not None else "Hardware"
        application_text = Text(str(row.get("application") or "—"))
        if application_text.plain.casefold() == "unknown":
            application_text.stylize("dim")

        info = row.get("stateDetail") or row.get("applicationDetail") or row.get("detail")
        if info and str(info).startswith("Pass --profile with matching firmware"):
            info = None
        connection_text = Text(_connection(row))
        if info:
            if connection_text.plain != "—":
                connection_text.append("\n")
                connection_text.append(str(info), style="dim")
            else:
                connection_text = Text(str(info))

        if compact:
            device_text = Text(_display_name(row))
            device_text.append(f"\n{kind}", style="bright_blue")
            details_text = Text("Application: ")
            details_text.append(application_text)
            if connection_text.plain != "—":
                details_text.append("\n")
                details_text.append(connection_text)
            table.add_row(_status(row.get("status")), device_text, details_text)
        else:
            table.add_row(_status(row.get("status")), Text(kind, style="bright_blue"),
                          Text(_display_name(row)), application_text, connection_text)

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
