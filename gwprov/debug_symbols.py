"""Resolve local firmware symbols without guessing across releases or builds."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def retro_go_profile_symbols(version: str | None) -> tuple[Path, list[Path]] | None:
    """Return a managed profile's firmware/app ELFs for an exact version match."""
    if not version:
        return None
    from .profiles import profile_directory

    root = profile_directory()
    matches = []
    for candidate in sorted(root.iterdir(), key=lambda item: item.name.casefold()) if root.is_dir() else ():
        if not candidate.is_dir():
            continue
        metadata_path = candidate / "provision.json"
        firmware_elf = candidate / "debug" / "retro-go-debug.elf"
        if not metadata_path.is_file() or not firmware_elf.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        firmware = metadata.get("firmware") if isinstance(metadata, dict) else None
        if not isinstance(firmware, dict) or firmware.get("version") != version:
            retro_go = metadata.get("retroGo") if isinstance(metadata, dict) else None
            firmware = retro_go.get("firmware") if isinstance(retro_go, dict) else None
        installed_version = firmware.get("version") if isinstance(firmware, dict) else None
        if installed_version != version:
            continue
        app_symbols = sorted((candidate / "debug" / "apps").rglob("*.elf"),
                             key=lambda item: item.as_posix().casefold())
        matches.append((candidate, firmware_elf, app_symbols))
    if not matches:
        return None

    firmware_hashes = {
        hashlib.sha256(firmware_elf.read_bytes()).hexdigest()
        for _, firmware_elf, _ in matches
    }
    if len(firmware_hashes) != 1:
        # A release tag alone cannot distinguish different repositories or
        # rebuilds that reuse the same version string. Never choose by path order.
        return None

    def app_hashes(candidate: Path, paths: list[Path]) -> dict[str, tuple[str, Path]]:
        root = candidate / "debug" / "apps"
        result = {}
        for path in paths:
            relative = path.relative_to(root).as_posix()
            result[relative] = (hashlib.sha256(path.read_bytes()).hexdigest(), path)
        return result

    app_maps = [app_hashes(candidate, paths) for candidate, _, paths in matches]
    common_apps = {}
    for relative, (digest, path) in app_maps[0].items():
        if all(mapping.get(relative, (None, None))[0] == digest for mapping in app_maps[1:]):
            common_apps[relative] = path
    return matches[0][1], [common_apps[name] for name in sorted(common_apps)]
