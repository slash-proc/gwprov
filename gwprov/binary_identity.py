"""Pin executable identity to an open file, including a live Linux inode."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import sys


def file_identity(path):
    """Hash a stable open inode; reject replacement/edits while reading it."""
    path = Path(path)
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        checksum = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
        after = os.fstat(stream.fileno())
    fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if fingerprint(before) != fingerprint(after):
        raise RuntimeError(f"executable changed while hashing: {path}")
    return {"sha256": checksum.hexdigest(), "bytes": before.st_size,
            "device": before.st_dev, "inode": before.st_ino}


def process_binary_identity(pid):
    """Linux hashes /proc/PID/exe rather than a possibly replaced pathname."""
    import psutil
    process = psutil.Process(pid)
    executable = process.exe()
    if sys.platform.startswith("linux"):
        source = Path(f"/proc/{pid}/exe")
        identity = file_identity(source)
        identity["identitySource"] = "live-process-inode"
    else:
        source = Path(executable)
        identity = file_identity(source)
        current = source.stat()
        if (current.st_dev, current.st_ino, current.st_size) != (
                identity["device"], identity["inode"], identity["bytes"]):
            raise RuntimeError(f"executable pathname changed during process identification: {source}")
        identity["identitySource"] = "verified-executable-path"
    return {"executable": executable, **identity, "argv": process.cmdline()}
