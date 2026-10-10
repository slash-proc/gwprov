"""Select architecture-appropriate binutils for an exact ELF image."""
from __future__ import annotations
from functools import lru_cache
from pathlib import Path
import shutil
import subprocess
from elftools.elf.elffile import ELFFile

_PREFIXES = {
    'EM_ARM': ('arm-none-eabi-', 'arm-linux-gnueabihf-', ''),
    'EM_AARCH64': ('aarch64-none-elf-', 'aarch64-linux-gnu-', ''),
    'EM_RISCV': ('riscv64-unknown-elf-', 'riscv64-linux-gnu-', ''),
    'EM_X86_64': ('', 'x86_64-linux-gnu-'),
    'EM_386': ('', 'i686-linux-gnu-'),
}


@lru_cache(maxsize=32)
def _version(path, size, mtime_ns):
    result = subprocess.run([path, '--version'], check=True, capture_output=True, text=True)
    return result.stdout.splitlines()[0] if result.stdout else 'version not reported'


def select_elf_tool(elf, operation='objdump'):
    """Inspect ELF machine; never prefer ARM tools for a host executable.

    Native GNU binutils may lack a foreign target. Such invocation failures
    remain explicit; selection is not a guarantee of compiled target support.
    """
    if operation not in ('objdump', 'addr2line'):
        raise ValueError(f'unsupported ELF operation: {operation}')
    elf = Path(elf)
    with elf.open('rb') as stream:
        machine = ELFFile(stream)['e_machine']
    prefixes = _PREFIXES.get(machine)
    if prefixes is None:
        raise ValueError(f'unsupported ELF machine {machine}: {elf}')
    for prefix in prefixes:
        name = prefix + operation
        executable = shutil.which(name)
        if executable:
            executable = str(Path(executable).resolve())
            stat = Path(executable).stat()
            return {'machine': machine, 'operation': operation, 'path': executable,
                    'version': _version(executable, stat.st_size, stat.st_mtime_ns)}
    raise FileNotFoundError(f'no {operation} available for ELF machine {machine}: {elf}')
