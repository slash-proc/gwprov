"""Read and resolve the TOML device profiles used by GWemu."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .common.target import Image


@dataclass(frozen=True)
class DeviceProfile:
    """A GWemu profile's firmware and storage files, resolved from profile.toml."""

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
        root = Path(directory).expanduser().resolve()
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
