"""Resolve managed device profiles and their packaged image files."""

from __future__ import annotations

import json
import os
import shutil
import sys

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .common.target import Image


PROFILE_DIR_ENV = "GWPROV_PROFILE_DIR"
_FICLONE = 0x40049409


def profile_directory(override: str | Path | None = None) -> Path:
    """Return the managed profile root, honoring an explicit or environment override."""
    configured = override if override is not None else os.environ.get(PROFILE_DIR_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = Path(xdg_data_home).expanduser()
    elif os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data).expanduser() if local_app_data else Path.home() / "AppData/Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path.home() / ".local/share"
    return (base / "gwprov/profiles").resolve()


def _is_path_reference(value: str) -> bool:
    return bool(Path(value).is_absolute() or PureWindowsPath(value).drive
                or "/" in value or "\\" in value or value.startswith("~"))


def resolve_profile_path(profile: str | Path, *, profile_dir: str | Path | None = None) -> Path:
    """Resolve explicit paths directly and bare profile names under the managed root."""
    value = os.fspath(profile)
    path = Path(value).expanduser()
    if _is_path_reference(value):
        return path.resolve()
    if value in {"", ".", ".."}:
        raise ValueError("profile name must be a non-empty directory name")
    return (profile_directory(profile_dir) / value).resolve()


def profile_destination(name_or_path: str | Path, *, output_dir: str | Path | None = None) -> Path:
    """Resolve a create destination; --output-dir requires a single profile name."""
    value = os.fspath(name_or_path)
    if output_dir is not None:
        if _is_path_reference(value) or value in {"", ".", ".."}:
            raise ValueError("--output-dir requires a profile name without a directory path")
        return (profile_directory(output_dir) / value).resolve()
    return resolve_profile_path(name_or_path)


def list_profiles(*, profile_dir: str | Path | None = None) -> list[dict[str, str]]:
    """List valid named profiles in the managed profile directory."""
    root = profile_directory(profile_dir)
    if not root.is_dir():
        return []
    rows = []
    for directory in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not directory.is_dir() or not (directory / "profile.toml").is_file():
            continue
        try:
            profile = DeviceProfile.load(directory)
            required_files = [profile.bank1, profile.bank2, profile.extflash]
            if profile.resolved_sd:
                required_files.append(profile.resolved_sd)
            missing = [str(path) for path in required_files if not path.is_file()]
            status = "incomplete" if missing else "ready"
            error = "Missing image files: " + ", ".join(missing) if missing else ""
            display_name = profile.display_name
        except (OSError, RuntimeError, ValueError) as exc:
            status = "invalid"
            error = str(exc)
            display_name = directory.name
        details = {}
        metadata = directory / "provision.json"
        if metadata.is_file():
            try:
                details = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                details = {}
        rows.append({
            "name": directory.name,
            "display_name": display_name,
            "path": str(directory.resolve()),
            "status": status,
            "error": error,
            "model": str(details.get("model", "")),
            "firmware": str(details.get("firmware", "")),
        })
    return rows


def _copy_independent_file(source: str | Path, destination: str | Path) -> str:
    """Copy one file, using a private copy-on-write clone where Linux supports it."""
    source_path = Path(source)
    destination_path = Path(destination)
    if sys.platform.startswith("linux"):
        try:
            import fcntl

            with source_path.open("rb") as source_file:
                with destination_path.open("xb") as destination_file:
                    fcntl.ioctl(destination_file.fileno(), _FICLONE, source_file.fileno())
            shutil.copystat(source_path, destination_path, follow_symlinks=True)
            return str(destination_path)
        except OSError:
            destination_path.unlink(missing_ok=True)
    return shutil.copy2(source_path, destination_path, follow_symlinks=True)


def duplicate_profile(source: str | Path, destination: str | Path, *,
                      output_dir: str | Path | None = None) -> dict[str, str | int]:
    """Create an independent working copy of a complete profile directory."""
    source_path = resolve_profile_path(source)
    destination_path = profile_destination(destination, output_dir=output_dir)
    manifest = source_path / "profile.toml"
    if not source_path.is_dir() or not manifest.is_file():
        raise ValueError(f"profile does not contain profile.toml: {source_path}")
    try:
        tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"profile manifest is invalid: {manifest}: {exc}") from exc
    if source_path == destination_path:
        raise ValueError("source and destination profile must be different")
    if source_path in destination_path.parents:
        raise ValueError("destination profile cannot be inside the source profile")
    if destination_path.exists():
        raise ValueError(f"destination profile already exists: {destination_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(
            source_path,
            destination_path,
            symlinks=False,
            copy_function=_copy_independent_file,
        )
    except BaseException:
        if destination_path.exists():
            shutil.rmtree(destination_path)
        raise

    files = sum(1 for path in destination_path.rglob("*") if path.is_file())
    return {
        "source": str(source_path),
        "destination": str(destination_path),
        "files": files,
    }


@dataclass(frozen=True)
class DeviceProfile:
    """A device profile's firmware and storage files, resolved from profile.toml."""

    root: Path
    display_name: str
    bank1: Path
    bank2: Path
    extflash: Path
    sd_mode: str = "none"
    sd_image: str = ""
    provenance: dict[str, str] | None = None
    extra: dict[str, Any] | None = None
    resolved_sd: Path | None = None

    @classmethod
    def load(cls, directory: str | Path, *, shared_sd_root: str | Path | None = None):
        root = resolve_profile_path(directory)
        manifest = root / "profile.toml"
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        flash = data.get("flash", {})
        sd = data.get("sd", {})

        def local_file(name: str, fallback: str) -> Path:
            path = (root / flash.get(name, fallback)).resolve()
            if root not in path.parents:
                raise ValueError(f"profile {name} path escapes its profile directory")
            return path

        sd_mode = sd.get("mode", "none")
        sd_image = sd.get("image", "")
        resolved_sd = None
        if sd_mode == "bundled":
            resolved_sd = (root / sd_image).resolve()
            if root not in resolved_sd.parents:
                raise ValueError("profile SD path escapes its profile directory")
        elif sd_mode == "shared":
            if shared_sd_root is None:
                raise ValueError("profile uses a shared SD image; provide shared_sd_root")
            sd_root = Path(shared_sd_root).expanduser().resolve()
            resolved_sd = (sd_root / sd_image).resolve()
            if sd_root not in resolved_sd.parents:
                raise ValueError("shared SD path escapes its SD-card directory")
        elif sd_mode != "none":
            raise ValueError(f"unknown SD mode {sd_mode!r}")

        return cls(
            root=root,
            display_name=data.get("display_name", root.name),
            bank1=local_file("bank1", "bank1.bin"),
            bank2=local_file("bank2", "bank2.bin"),
            extflash=local_file("extflash", "extflash.bin"),
            sd_mode=sd_mode,
            sd_image=sd_image,
            provenance=flash.get("provenance", {}),
            extra={key: value for key, value in data.items()
                   if key not in {"version", "display_name", "created", "flash", "sd"}},
            resolved_sd=resolved_sd,
        )

    def image(self, *, intflash_bank: int = 1) -> Image:
        """Create a target-neutral image model from this profile."""
        from .common.target import Image

        if intflash_bank not in (1, 2):
            raise ValueError("intflash_bank must be 1 or 2")
        return Image(
            bank1=str(self.bank1),
            bank2=str(self.bank2),
            extflash=str(self.extflash),
            sdcard=str(self.resolved_sd) if self.resolved_sd else "",
            intflash_bank=intflash_bank,
        )
