"""Lossless developer ELF compaction; flashed/loadable bytes remain unchanged."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from elftools.elf.elffile import ELFFile
from .binary_identity import file_identity


def load_segment_identity(path):
    with Path(path).open("rb") as stream:
        image = ELFFile(stream)
        return [{"vaddr": int(segment["p_vaddr"]), "paddr": int(segment["p_paddr"]),
                 "filesz": int(segment["p_filesz"]), "memsz": int(segment["p_memsz"]),
                 "flags": int(segment["p_flags"]), "align": int(segment["p_align"]),
                 "sha256": hashlib.sha256(segment.data()).hexdigest()}
                for segment in image.iter_segments() if segment["p_type"] == "PT_LOAD"]


def compact_debug(source, output):
    """Write a separate SHF_COMPRESSED ELF and verify every PT_LOAD byte/address."""
    source = Path(source).expanduser().resolve(strict=True)
    output = Path(output).expanduser().resolve()
    if source == output or output.exists():
        raise FileExistsError(f"refusing to replace an ELF: {output}")
    tool = shutil.which("arm-none-eabi-objcopy") or shutil.which("objcopy")
    if not tool:
        raise FileNotFoundError("arm-none-eabi-objcopy or objcopy is required")
    original = file_identity(source)
    before = load_segment_identity(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.{os.getpid()}.partial")
    if partial.exists():
        raise FileExistsError(partial)
    try:
        subprocess.run([tool, "--compress-debug-sections=zlib-gabi", str(source), str(partial)],
                       check=True, capture_output=True, text=True)
        if load_segment_identity(partial) != before:
            raise RuntimeError("debug compression changed loadable ELF segments")
        if file_identity(source)["sha256"] != original["sha256"]:
            raise RuntimeError("source ELF changed during compression")
        with partial.open("rb") as stream:
            image = ELFFile(stream)
            dwarf = image.get_dwarf_info()
            next(dwarf.iter_CUs(), None)  # Prove native SHF_COMPRESSED decoding.
        compact = file_identity(partial)
        partial.replace(output)
        return {"source": str(source), "output": str(output), "compression": "zlib-gabi",
                "source_sha256": original["sha256"], "output_sha256": compact["sha256"],
                "source_bytes": original["bytes"], "output_bytes": compact["bytes"],
                "load_segments_identical": True, "load_segments": before,
                "saved_bytes": original["bytes"] - compact["bytes"]}
    finally:
        partial.unlink(missing_ok=True)
