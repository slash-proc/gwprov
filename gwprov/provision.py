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
                   sd_size_mib: int = 128, sd_label: str = "RETROGO",
                   bootloader_repo: str = "sylverb/game-and-watch-bootloader",
                   bootloader_version: str = "v1.0.8",
                   bootloader_file: str | Path | None = None) -> dict:
    source = Path(content).expanduser().resolve()
    metadata = json.loads((source / '.gwprov-firmware.json').read_text())
    variant = metadata.get('variant')
    if variant not in {'flash', 'sd'}:
        raise ValueError('firmware metadata variant must be flash or sd; run gwprov retro-go install first')
    if sd_size_mib < 2:
        raise ValueError('SD image must be at least 2 MiB')
    declared_files = metadata.get('files')
    if not isinstance(declared_files, list) or not declared_files:
        raise ValueError('firmware metadata has no declared content files; run gwprov retro-go install first')
    missing_files = []
    for relative in declared_files:
        if not isinstance(relative, str):
            raise ValueError('firmware metadata contains a non-path file entry')
        parts = Path(relative).parts
        if not parts or parts[0] not in {'firmware', variant} or '..' in parts:
            raise ValueError(f'invalid firmware content path: {relative!r}')
        if not (source / relative).is_file():
            missing_files.append(relative)
    if missing_files:
        preview = ', '.join(missing_files[:4])
        suffix = ' ...' if len(missing_files) > 4 else ''
        raise ValueError(f'firmware content is incomplete; missing {preview}{suffix}; rerun gwprov retro-go install')
    projects_path = source / variant / '.gwprov-projects.json'
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
    if variant == 'flash' and not 1 <= littlefs_mib < (extflash_mib or 256):
        raise ValueError('LittleFS must be at least 1 MiB and smaller than the extflash chip')
    from .bootloader import resolve_bootloader
    bootloader, boot_info = resolve_bootloader(
        cache=source.parent / '.gwprov-cache', repo=bootloader_repo,
        version=bootloader_version, local=bootloader_file)
    root.parent.mkdir(parents=True, exist_ok=True)
    if variant == 'sd':
        chip_mib = select_extflash_mib(0, extflash_mib or 64)
        total = chip_mib * MIB
        firmware = patch_layout((source/'firmware/intflash.bin').read_bytes(),
                                metadata['firmware']['superblock'], frogfs_length=0,
                                extflash_size=total, littlefs_length=0)
        if len(firmware) > BANK_SIZE:
            raise ValueError('firmware is larger than bank 2')
        with tempfile.TemporaryDirectory(prefix='.gwprov-profile-', dir=root.parent) as tmp:
            work = Path(tmp)
            instance = work / 'instance'
            instance.mkdir()
            _write_profile_images(instance, source, metadata, bootloader, firmware,
                                  extflash_bytes=total)
            project_symbols = _copy_project_symbols(source, projects, instance)
            sd_image = instance / 'sdcard.img'
            from .common.sdcard import create_image, push_tree, QemuSDCardManager
            create_image(str(sd_image), size_mb=sd_size_mib, label=sd_label)
            push_tree(QemuSDCardManager(str(sd_image)), source / 'sd',
                      exclude_names={'.gwprov-projects.json'})
            display_name = name or root.name
            _write_profile_config(instance, display_name, sd_mode='bundled')
            report = {'firmware': metadata, 'projects': projects,
                      'debugSymbols': 'debug/retro-go-debug.elf' if metadata.get('debugSymbols') else None,
                      'projectSymbols': project_symbols,
                      'layout': {'extflashBytes': total,
                                 'sdImageBytes': sd_image.stat().st_size,
                                 'sdLabel': sd_label},
                      'boot': 'official bootloader at 0x08000000', 'bootloader': boot_info,
                      'variant': 'sd'}
            (instance/'provision.json').write_text(json.dumps(report, indent=2)+'\n')
            (instance/'gwemu.toml').write_text('[general]\nshow_welcome = false\n')
            instance.rename(root)
        return report

    from .vendor.retrogo_sd.scripts import gen_frogfs_image, gen_littlefs_image
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
        config_file = littlefs_tree / 'CONFIG'
        if config_file.is_file():
            lfs_args.extend(['--include-root-file', 'CONFIG'])
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
        _write_profile_images(instance, source, metadata, bootloader, firmware)
        project_symbols = _copy_project_symbols(source, projects, instance)
        with (instance/'extflash.bin').open('wb') as output:
            for _ in range(chip_mib):output.write(b'\xff'*MIB)
            output.seek(0);output.write(frogfs)
            output.seek(total-littlefs_size)
            with (work/'littlefs.bin').open('rb') as packed:shutil.copyfileobj(packed,output)
        display_name = name or root.name
        _write_profile_config(instance, display_name, sd_mode='none')
        report = {'firmware': metadata, 'projects': projects,
                  'debugSymbols': 'debug/retro-go-debug.elf' if metadata.get('debugSymbols') else None,
                  'projectSymbols': project_symbols,
                  'layout': {'extflashBytes': total, 'frogfsBytes': len(frogfs),
                             'littlefsOffset': total-littlefs_size, 'littlefsBytes': littlefs_size},
                  'boot': 'official bootloader at 0x08000000', 'bootloader': boot_info}
        (instance/'provision.json').write_text(json.dumps(report,indent=2)+'\n')
        (instance/'gwemu.toml').write_text('[general]\nshow_welcome = false\n')
        instance.rename(root)
    return report


def _write_profile_images(instance: Path, source: Path, metadata: dict,
                          bootloader: bytes, firmware: bytes, *,
                          extflash_bytes: int | None = None) -> None:
    symbol_relative = metadata.get('debugSymbols')
    if symbol_relative:
        symbols = (source / symbol_relative).resolve()
        if source not in symbols.parents or not symbols.is_file():
            raise ValueError(f'firmware debug symbols are missing or escape the content root: {symbol_relative}')
        debug_dir = instance / 'debug'
        debug_dir.mkdir()
        shutil.copyfile(symbols, debug_dir / 'retro-go-debug.elf')
    (instance/'bank1.bin').write_bytes(bootloader.ljust(BANK_SIZE, b'\xff'))
    (instance/'bank2.bin').write_bytes(firmware.ljust(BANK_SIZE,b'\xff'))
    (instance/'rdp-state.bin').write_bytes(b'\xaa')
    if extflash_bytes is not None:
        with (instance/'extflash.bin').open('wb') as output:
            chunk = b'\xff' * MIB
            remaining = extflash_bytes
            while remaining:
                amount = min(remaining, MIB)
                output.write(chunk[:amount])
                remaining -= amount


def _copy_project_symbols(source: Path, projects: dict, instance: Path) -> list[str]:
    copied: list[str] = []
    for project in projects.values():
        if not isinstance(project, dict):
            continue
        for raw in project.get('symbols', []):
            relative = Path(raw)
            if relative.is_absolute() or not relative.parts or '..' in relative.parts:
                raise ValueError(f'invalid project symbol path in ownership marker: {raw!r}')
            symbol = (source / 'debug' / relative).resolve()
            debug_root = (source / 'debug' / 'projects').resolve()
            if debug_root not in symbol.parents or not symbol.is_file():
                raise ValueError(f'project debug symbols are missing or escape content root: {raw}')
            destination = instance / 'debug' / 'apps' / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(symbol, destination)
            copied.append(destination.relative_to(instance).as_posix())
    return sorted(copied)


def _write_profile_config(instance: Path, display_name: str, *, sd_mode: str) -> None:
    sd_config = ('[sd]\nmode = "bundled"\nimage = "sdcard.img"\n'
                 if sd_mode == 'bundled' else '[sd]\nmode = "none"\n')
    (instance/'profile.toml').write_text(
        f'version = 1\ndisplay_name = {json.dumps(display_name)}\n'
        '[flash]\nbank1 = "bank1.bin"\nbank2 = "bank2.bin"\nextflash = "extflash.bin"\n'
        + sd_config)
