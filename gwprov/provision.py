"""Assemble provisioned content into a self-contained, mutable GWemu instance."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import tempfile
import zlib

MIB = 1024 * 1024
BANK_SIZE = 256 * 1024


def patch_layout(firmware: bytes, declaration: dict, *, frogfs_length: int,
                 extflash_size: int, littlefs_length: int) -> bytes:
    if declaration != {'magic': 'GWLB', 'version': 2, 'structSize': 36}:
        raise ValueError(f'unsupported firmware layout declaration: {declaration}')
    candidates = [offset for offset in range(0, len(firmware) - 35, 4)
                  if firmware[offset:offset+8] == b'GWLB\x02\x00\x24\x00']
    if len(candidates) != 1:
        raise ValueError(f'expected one firmware layout superblock, found {len(candidates)}')
    result = bytearray(firmware)
    offset = candidates[0]
    flags = struct.unpack_from('<I', result, offset + 28)[0] | 15
    struct.pack_into('<6I', result, offset + 8, 0, frogfs_length, extflash_size,
                     0, littlefs_length, flags)
    struct.pack_into('<I', result, offset + 32, zlib.crc32(result[offset:offset+32]))
    return bytes(result)


def select_extflash_mib(required_bytes: int, requested: int | None = None) -> int:
    """Choose the smallest supported chip that fits, defaulting to at least 64 MiB."""
    sizes = (64, 128, 256)
    if requested is not None and requested not in sizes:
        raise ValueError('extflash size must be 64, 128 or 256 MiB')
    for size in ((requested,) if requested is not None else sizes):
        if required_bytes <= size * MIB:
            return size
    raise ValueError(f'content needs {required_bytes} bytes; exceeds ' +
                     (f'the selected {requested} MiB chip' if requested else 'the maximum 256 MiB chip'))


def create_profile(directory: str | Path, *, content: str | Path,
                   name: str | None = None, littlefs_mib: int = 2,
                   extflash_mib: int | None = None,
                   bootloader_repo: str = "sylverb/game-and-watch-bootloader",
                   bootloader_version: str = "v1.0.8",
                   bootloader_file: str | Path | None = None) -> dict:
    from .vendor.retrogo_sd.scripts import gen_frogfs_image, gen_littlefs_image

    source = Path(content).expanduser().resolve()
    metadata = json.loads((source / '.gwprov-firmware.json').read_text())
    if metadata.get('variant') != 'flash':
        raise ValueError('profile create currently assembles flash content; SD media uses the sd commands')
    projects_path = source / 'flash/.gwprov-projects.json'
    projects = json.loads(projects_path.read_text()) if projects_path.is_file() else {}
    abi = metadata['firmware']['providesAbi']
    mapped = []
    for project in projects.values():
        required = project.get('requiresAbi') or {}
        if required and (required.get('version') != abi['version'] or required.get('minSize', 0) > abi['size']):
            raise ValueError(f"project ABI is incompatible with firmware: {project['repo']}")
        mapped.extend(project.get('mapped', []))
    root = Path(directory).expanduser().resolve()
    if root.exists():
        raise ValueError(f'profile already exists; select a new instance directory: {root}')
    select_extflash_mib(0, extflash_mib)
    if not 1 <= littlefs_mib < (extflash_mib or 256):
        raise ValueError('LittleFS must be at least 1 MiB and smaller than the extflash chip')
    from .bootloader import resolve_bootloader
    bootloader, boot_info = resolve_bootloader(
        cache=source.parent / '.gwprov-cache', repo=bootloader_repo,
        version=bootloader_version, local=bootloader_file)
    root.parent.mkdir(parents=True, exist_ok=True)
    total = (extflash_mib or 256) * MIB
    littlefs_size = littlefs_mib * MIB
    with tempfile.TemporaryDirectory(prefix='.gwprov-profile-', dir=root.parent) as tmp:
        work = Path(tmp)
        frogfs_tree = source / 'flash/frogfs'
        littlefs_tree = work / 'littlefs-content'
        if (source / 'flash/littlefs').is_dir():
            shutil.copytree(source / 'flash/littlefs', littlefs_tree)
        common = ['--retro-go-root', str(work), '--roms-dir', str(work/'no-extra-roms')]
        frog_args = [*common, '--mkfrogfs', str(Path(__file__).parent/'vendor/frogfs/mkfrogfs.py'),
                     '--sd-content', str(frogfs_tree), '--output', str(work/'frogfs.bin'),
                     '--build-dir', str(work/'frogfs-build'), '--reserve-size', str(total-littlefs_size),
                     '--no-gencovers']
        for item in ('bios', 'covers', 'fonts', 'roms'):
            frog_args.extend(['--include', item])
        for item in mapped:
            path = Path(item['path'])
            if not path.parts or path.parts[0] != 'cores' or '..' in path.parts or path.is_absolute():
                raise ValueError(f'invalid mapped artifact path: {path}')
            base = item['relocBase']
            if isinstance(base, str): base = int(base, 0)
            if not isinstance(base, int): raise ValueError('mapped artifact has no relocation base')
            frog_args.extend(['--mapped-artifact', f"{frogfs_tree/path}:{path.relative_to('cores')}:{base}"])
        if gen_frogfs_image.main(frog_args):
            raise ValueError('FrogFS build failed')
        littlefs_tree.mkdir(parents=True, exist_ok=True)
        (littlefs_tree/'data').mkdir(exist_ok=True)
        lfs_args = [*common, '--sd-content', str(littlefs_tree), '--output', str(work/'littlefs.bin'),
                    '--build-dir', str(work/'littlefs-build'), '--size', str(littlefs_size),
                    '--block-size', str(metadata['littlefsBlockSize']), '--no-cores-filter']
        for item in ('cores', 'lang', 'data'):lfs_args.extend(['--include', item])
        if gen_littlefs_image.main(lfs_args):
            raise ValueError('LittleFS build failed')
        frogfs = (work/'frogfs.bin').read_bytes()
        chip_mib = select_extflash_mib(len(frogfs) + littlefs_size, extflash_mib)
        total = chip_mib * MIB
        if len(frogfs) > total-littlefs_size:
            raise ValueError(f'content needs {len(frogfs)} bytes; only {total-littlefs_size} fit before LittleFS')
        firmware = patch_layout((source/'firmware/intflash.bin').read_bytes(),
                                metadata['firmware']['superblock'], frogfs_length=len(frogfs),
                                extflash_size=total, littlefs_length=littlefs_size)
        if len(firmware) > BANK_SIZE:raise ValueError('firmware is larger than bank 2')
        instance = work/'instance'
        instance.mkdir()
        (instance/'bank1.bin').write_bytes(bootloader.ljust(BANK_SIZE, b'\xff'))
        (instance/'bank2.bin').write_bytes(firmware.ljust(BANK_SIZE,b'\xff'))
        with (instance/'extflash.bin').open('wb') as output:
            for _ in range(chip_mib):output.write(b'\xff'*MIB)
            output.seek(0);output.write(frogfs)
            output.seek(total-littlefs_size)
            with (work/'littlefs.bin').open('rb') as packed:shutil.copyfileobj(packed,output)
        display_name = name or root.name
        (instance/'profile.toml').write_text(
            f'version = 1\ndisplay_name = {json.dumps(display_name)}\n'
            '[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n'
            '[sd]\nmode = "none"\n')
        report = {'firmware': metadata, 'projects': projects,
                  'layout': {'extflashBytes': total, 'frogfsBytes': len(frogfs),
                             'littlefsOffset': total-littlefs_size, 'littlefsBytes': littlefs_size},
                  'boot': 'official bootloader at 0x08000000', 'bootloader': boot_info}
        (instance/'provision.json').write_text(json.dumps(report,indent=2)+'\n')
        (instance/'gwemu.toml').write_text('[general]\nshow_welcome = false\n')
        instance.rename(root)
    return report
