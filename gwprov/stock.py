"""Create pristine stock emulator profiles from verified user-owned backups."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import tempfile

MIB = 1024 * 1024
EXPECTED = {
    "mario": ("efa04c387ad7b40549e15799b471a6e1cd234c76", "eea70bb171afece163fb4b293c5364ddb90637ae"),
    "zelda": ("ac14bcea6e4ff68c88fd2302c021025a2fb47940", "1c1c0ed66d07324e560dcd9e86a322ec5e4c1e96"),
}


def create_stock_profile(directory, *, backup_dirs, locked=False, model="auto", extflash_mib=64):
    if extflash_mib not in (64, 128, 256):
        raise ValueError("extflash capacity must be 64, 128 or 256 MiB")
    root=Path(directory).expanduser().resolve()
    if root.exists():
        raise ValueError(f"profile already exists: {root}")
    chosen=None
    for variant in (("mario", "zelda") if model == "auto" else (model,)):
        if variant not in EXPECTED:
            raise ValueError("stock model must be mario, zelda or auto")
        for folder in backup_dirs:
            folder=Path(folder).expanduser().resolve()
            internal_path=folder/f"internal_flash_backup_{variant}.bin"
            external_path=folder/f"flash_backup_{variant}.bin"
            if not internal_path.is_file() or not external_path.is_file():
                continue
            if not 0x20000 <= internal_path.stat().st_size <= 256*1024 or external_path.stat().st_size > extflash_mib*MIB:
                continue
            internal=internal_path.read_bytes();external=external_path.read_bytes()
            check=external[:-8192] if variant == "mario" else external[0x20000:0x3254a0]
            if (hashlib.sha1(internal[:0x20000]).hexdigest(),hashlib.sha1(check).hexdigest()) == EXPECTED[variant]:
                chosen=(variant,internal,external);break
        if chosen:
            break
    if not chosen:
        raise ValueError("No hash-valid Mario or Zelda stock OFW backup pair found")
    variant,internal,external=chosen
    root.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".gwprov-stock-",dir=root.parent) as temporary:
        instance=Path(temporary)/"instance";instance.mkdir()
        (instance/"bank1.bin").write_bytes(internal.ljust(256*1024,b"\xff"))
        (instance/"bank2.bin").write_bytes(b"\xff"*(256*1024))
        with (instance/"extflash.bin").open("wb") as output:
            output.write(external)
            remaining=extflash_mib*MIB-len(external)
            while remaining:
                chunk=min(remaining,MIB);output.write(b"\xff"*chunk);remaining-=chunk
        (instance/"rdp-state.bin").write_bytes(bytes([0x55 if locked else 0xaa]))
        (instance/"profile.toml").write_text('version = 1\n[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n[sd]\nmode = "none"\n')
        (instance/"gwemu.toml").write_text('[general]\nshow_welcome = false\n')
        report={"firmware":"stock","model":variant,"locked":locked,"rdpFile":"rdp-state.bin",
                "internal_sha256":hashlib.sha256(internal).hexdigest(),
                "external_sha256":hashlib.sha256(external).hexdigest(),
                "layout":{"extflashBytes":extflash_mib*MIB}}
        (instance/"provision.json").write_text(json.dumps(report,indent=2)+"\n")
        instance.rename(root)
    return report
