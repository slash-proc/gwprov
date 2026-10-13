"""Loopback port allocation for local GWemu services.

GWemu's GDB and QMP listeners are local implementation details, so automatic
allocation should be predictable and should never reuse the conventional
default itself.  We probe candidates with the same address family and address
that the launchers use, starting at ``default + 1`` and moving upward.
"""

from __future__ import annotations

import socket
from collections.abc import Iterable


# These are the conventional bases, not ports that automatic allocation uses.
# The first automatic GDB and QMP candidates are therefore 1235 and 4445.
DEFAULT_GDB_PORT = 1234
DEFAULT_QMP_PORT = 4444

_RESERVED_PORTS: set[int] = set()


def _validate_port(port: int, label: str = "port") -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"{label} must be an integer from 1 to 65535")
    return port


def port_is_available(port: int, *, host: str = "127.0.0.1") -> bool:
    """Return whether a TCP listener can bind ``host:port`` right now."""
    _validate_port(port)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(default: int, *, host: str = "127.0.0.1",
                   reserved: Iterable[int] = ()) -> int:
    """Find a free port beginning at ``default + 1``.

    ``reserved`` keeps two services selected during one launch from receiving
    the same candidate after the first probe socket has been closed.
    """
    _validate_port(default, "default port")
    reserved_ports = _RESERVED_PORTS | set(reserved)
    for candidate in range(default + 1, 65536):
        if candidate in reserved_ports:
            continue
        if port_is_available(candidate, host=host):
            return candidate
    raise RuntimeError(f"no free TCP port found after {host}:{default}")


def validate_requested_port(port: int, *, host: str = "127.0.0.1",
                            reserved: Iterable[int] = ()) -> int:
    """Validate an explicit launch port and fail before starting GWemu."""
    _validate_port(port)
    if port in (_RESERVED_PORTS | set(reserved)):
        raise ValueError(f"TCP port {port} is already assigned to another GWemu service")
    if not port_is_available(port, host=host):
        raise RuntimeError(f"TCP port {port} on {host} is already in use")
    return port


def allocate_gwemu_ports(*, gdb_port: int | None = None,
                         qmp_port: int | None = None,
                         host: str = "127.0.0.1") -> tuple[int, int]:
    """Choose distinct GDB and QMP ports for a direct GWemu launch."""
    if gdb_port is None:
        selected_gdb = find_free_port(DEFAULT_GDB_PORT, host=host)
    else:
        selected_gdb = validate_requested_port(gdb_port, host=host)
    if qmp_port is None:
        selected_qmp = find_free_port(DEFAULT_QMP_PORT, host=host,
                                      reserved=(selected_gdb,))
    else:
        selected_qmp = validate_requested_port(qmp_port, host=host,
                                               reserved=(selected_gdb,))
    _RESERVED_PORTS.update((selected_gdb, selected_qmp))
    return selected_gdb, selected_qmp


def release_ports(*ports: int) -> None:
    """Release ports reserved by an in-process direct GWemu target."""
    _RESERVED_PORTS.difference_update(ports)
