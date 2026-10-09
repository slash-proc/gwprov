"""Small, predictable renderers for human-facing CLI output."""
from __future__ import annotations

from pathlib import Path
import textwrap
from typing import Any


def _wrap_field(label: str, value: Any, *, width: int, indent: str = "    ") -> list[str]:
    available = max(20, width - len(indent) - len(label) - 2)
    lines = textwrap.wrap(str(value), width=available, break_long_words=True,
                          break_on_hyphens=False) or [""]
    return [f"{indent}{label}: {lines[0]}"] + [f"{indent}{' ' * len(label)}  {line}"
                                                  for line in lines[1:]]


def _terminal_width() -> int:
    try:
        import shutil
        return max(60, shutil.get_terminal_size((100, 24)).columns)
    except OSError:
        return 100


def _profile_name(value: Any) -> str:
    if not value:
        return ""
    return Path(str(value)).expanduser().name or str(value)


def render_process_list(rows: list[dict[str, Any]], *, title: str = "Devices",
                        width: int | None = None) -> str:
    """Render inventory rows as compact, terminal-width-aware entries."""
    if not rows:
        return f"{title}\nNo devices found."

    width = width or _terminal_width()
    lines = [f"{title} ({len(rows)})"]
    sections: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        section = "GWemu" if row.get("kind") == "gwemu" or row.get("pid") is not None else "Hardware"
        sections.setdefault(section, []).append(row)

    for section, items in sections.items():
        lines.extend(("", section))
        for row in items:
            status = str(row.get("status", "unknown")).upper()
            name = str(row.get("name") or row.get("pid") or "Unnamed device")
            if section == "GWemu":
                name = _profile_name(row.get("profile") or name)
            lines.append(f"  {status}  {name}")

            if row.get("pid") is not None:
                details = [f"PID {row['pid']}"]
                if "display" in row:
                    details.append(f"Display {'on' if row['display'] else 'off'}")
                if row.get("gdbPort"):
                    details.append(f"GDB :{row['gdbPort']}")
                lines.extend(_wrap_field("Process", " · ".join(details), width=width))
            elif row.get("probeId"):
                probe = row.get("vendor") or row.get("backend")
                lines.extend(_wrap_field("Probe", f"{probe} · {row['probeId']}", width=width))

            application = row.get("application")
            if application:
                lines.extend(_wrap_field("Application", application, width=width))
            detail = row.get("stateDetail") or row.get("applicationDetail") or row.get("detail")
            # The unified inventory's ordinary no-symbol hint is actionable but
            # redundant on every healthy row; keep it as one footer below.
            if detail and not (section == "Hardware" and
                               str(detail).startswith("Pass --profile with matching firmware")):
                lines.extend(_wrap_field("Info", detail, width=width))
    if any(row.get("kind") == "hardware" and row.get("status") in {"running", "halted"}
           and row.get("application") == "Unknown" for row in rows):
        lines.append("")
        lines.extend(textwrap.wrap(
            "Use --profile to resolve application state from firmware and app symbols.",
            width=width, break_long_words=True, break_on_hyphens=False))
    return "\n".join(lines)
