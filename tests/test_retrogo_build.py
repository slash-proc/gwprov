import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from gwprov.common import retrogo_build


class RetroGoBuild(unittest.TestCase):
    def test_variant_selects_release_or_filesystem_make_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Makefile.common").touch()
            with patch("gwprov.common.retrogo_build.subprocess.run") as run:
                run.return_value.returncode = 0
                sd_commands = retrogo_build.build(
                    path=root, variant="sd", dry_run=True, verbose=False
                )
                flash_commands = retrogo_build.build(
                    path=root, variant="flash", dry_run=True, verbose=False
                )
            self.assertEqual(sd_commands[-1], "release")
            self.assertEqual(flash_commands[-3:], ["all", "frogfs_image", "littlefs_image"])
            self.assertIn("SD_CARD=1", sd_commands)
            self.assertIn("SD_CARD=0", flash_commands)

    def test_stage_build_writes_download_compatible_content_and_hash_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            build_dir = source / "build"
            build_dir.mkdir(parents=True)
            intflash = build_dir / "firmware.bin"
            image = bytearray(b"\xff" * 128)
            image[16:24] = b"GWLB\x02\x00\x24\x00"
            struct.pack_into("<6I", image, 24, 16 * 1024 * 1024, 4, 64 * 1024 * 1024, 0, 4, 15)
            intflash.write_bytes(image)
            elf = build_dir / "firmware.elf"
            elf.write_bytes(b"\x7fELF-test")
            frog = build_dir / "frogfs.bin"
            frog.write_bytes(b"FROG")
            little = build_dir / "littlefs.bin"
            little.write_bytes(b"LFS!")
            fb = retrogo_build.FirmwareBuild(
                path=str(source),
                intflash_bank=2,
                intflash_bin=str(intflash),
                elf=str(elf),
                frogfs=str(frog),
                littlefs=str(little),
                variant="flash",
                params={"SD_CARD": 0},
            )
            with (
                patch(
                    "gwprov.common.retrogo_build._git_source",
                    return_value={
                        "repo": "owner/repo",
                        "version": "v-test",
                        "commit": "abc",
                        "dirty": False,
                    },
                ),
                patch("gwprov.common.retrogo_build._abi_metadata", return_value=(2, 908)),
            ):
                metadata = retrogo_build.stage_build(fb, root / "staged")
            staged = root / "staged"
            self.assertEqual((staged / "flash/frogfs.bin").read_bytes(), b"FROG")
            self.assertEqual(metadata["firmware"]["providesAbi"], {"version": 2, "size": 908})
            manifest = json.loads((staged / "manifest.json").read_text())
            self.assertEqual({item["path"] for item in manifest["files"]}, set(metadata["files"]))
            self.assertTrue(all(len(item["sha256"]) == 64 for item in manifest["files"]))

    def test_firmware_abi_parser_skips_objdump_addresses(self):
        result = type(
            "Result",
            (),
            {
                "returncode": 0,
                "stdout": "Contents of section .firmware_abi:\n 8000400 02000000 8c030000 31090008\n",
            },
        )()
        with patch("gwprov.common.retrogo_build.subprocess.run", return_value=result):
            self.assertEqual(retrogo_build._abi_metadata("firmware.elf"), (2, 908))


if __name__ == "__main__":
    unittest.main()
