import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
import zlib

from gwprov.dist.inputs import read_input
from gwprov.provision import create_profile, patch_layout


class Inputs(unittest.TestCase):
    def test_zip_uses_inner_basename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("folder/", b"")
                archive.writestr("folder/Actual Game.gba", b"game")
            self.assertEqual(read_input(path, extensions={".gba"}), ("Actual Game.gba", b"game"))

    def test_multiple_files_are_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("game.gba", b"game")
                archive.writestr("readme.txt", b"notes")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                read_input(path, extensions={".gba"})
            self.assertEqual(read_input(path, extensions={".zip"})[1], path.read_bytes())

    def test_unpacked_size_is_checked_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("game.gba", b"\0" * 10000)
            with self.assertRaisesRegex(ValueError, "unpacked input exceeds"):
                read_input(path, extensions={".gba"}, max_bytes=100)

    def test_zip_extension_and_compression_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.zip"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("notes.txt", b"notes")
            with self.assertRaisesRegex(ValueError, "expected extension"):
                read_input(path, extensions={".gba"})
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_BZIP2) as archive:
                archive.writestr("game.gba", b"game")
            with self.assertRaisesRegex(ValueError, "stored and deflate"):
                read_input(path, extensions={".gba"})


class Profile(unittest.TestCase):
    declaration = {"magic": "GWLB", "version": 2, "structSize": 36}

    def firmware(self):
        return (
            struct.pack("<II", 0x20020000, 0x08100101)
            + b"\0" * 56
            + struct.pack("<4sHH7I", b"GWLB", 2, 36, 0, 0, 0, 0, 0, 0, 0)
        )

    def test_layout_crc_matches_firmware_contract(self):
        output = patch_layout(
            self.firmware(),
            self.declaration,
            frogfs_length=1234,
            extflash_size=64 * 1024 * 1024,
            littlefs_length=2 * 1024 * 1024,
        )
        self.assertEqual(
            struct.unpack_from("<6I", output, 72),
            (0, 1234, 64 * 1024 * 1024, 0, 2 * 1024 * 1024, 15),
        )
        self.assertEqual(struct.unpack_from("<I", output, 96)[0], zlib.crc32(output[64:96]))

    def test_ambiguous_superblock_is_rejected(self):
        firmware = self.firmware()
        with self.assertRaisesRegex(ValueError, "found 2"):
            patch_layout(
                firmware + firmware[64:],
                self.declaration,
                frogfs_length=1,
                extflash_size=64 * 1024 * 1024,
                littlefs_length=2 * 1024 * 1024,
            )

    def test_real_filesystems_profile_and_source_preservation(self):
        from littlefs import LittleFS

        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            (content / "firmware").mkdir(parents=True)
            (content / "firmware/intflash.bin").write_bytes(self.firmware())
            (content / "flash/frogfs/roms/gba").mkdir(parents=True)
            (content / "flash/frogfs/roms/gba/game.gba").write_bytes(b"game")
            (content / "flash/littlefs/cores").mkdir(parents=True)
            (content / "flash/littlefs/cores/gba.bin").write_bytes(b"core")
            (content / ".gwprov-firmware.json").write_text(
                json.dumps(
                    {
                        "variant": "flash",
                        "littlefsBlockSize": 4096,
                        "files": ["firmware/intflash.bin"],
                        "firmware": {
                            "providesAbi": {"version": 2, "size": 908},
                            "superblock": self.declaration,
                        },
                    }
                )
            )
            before = {
                str(p.relative_to(content)): p.read_bytes()
                for p in content.rglob("*")
                if p.is_file()
            }
            profile = Path(tmp) / "profile"
            bootloader = Path(tmp) / "bootloader.bin"
            bootloader.write_bytes(struct.pack("<II", 0x20020000, 0x08000009) + b"\x00\xbf")
            report = create_profile(
                profile, content=content, littlefs_mib=1, bootloader_file=bootloader
            )
            self.assertEqual((profile / "extflash.bin").stat().st_size, 64 * 1024 * 1024)
            import yaml

            flash_map = yaml.safe_load((profile / "flash-map.yaml").read_text())
            frogfs_part = next(
                part for part in flash_map["bank0"] if part["name"] == "Retro-Go FrogFS"
            )
            self.assertEqual((profile / frogfs_part["file"]).read_bytes()[:4], b"FROG")
            self.assertEqual(int(frogfs_part["offset"]), report["layout"]["frogfsOffset"])
            self.assertEqual((profile / "bank2.bin").stat().st_size, 256 * 1024)
            self.assertEqual(
                before,
                {
                    str(p.relative_to(content)): p.read_bytes()
                    for p in content.rglob("*")
                    if p.is_file()
                },
            )
            with (profile / "extflash.bin").open("rb") as image:
                image.seek(report["layout"]["littlefsOffset"])
                reverse = image.read()
            linear = b"".join(reverse[i : i + 4096] for i in range(len(reverse) - 4096, -1, -4096))
            fs = LittleFS(
                block_size=4096,
                block_count=256,
                read_size=256,
                prog_size=256,
                cache_size=256,
                lookahead_size=16,
                mount=False,
            )
            fs.context.buffer = bytearray(linear)
            fs.mount()
            with fs.open("/cores/gba.bin", "rb") as f:
                self.assertEqual(f.read(), b"core")
            fs.unmount()
            with self.assertRaisesRegex(ValueError, "already exists"):
                create_profile(profile, content=content)

    def test_prebuilt_retro_go_images_are_composed_without_repacking(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            (content / "firmware").mkdir(parents=True)
            (content / "flash").mkdir()
            firmware = bytearray(self.firmware())
            # The prebuilt image is already configured for this exact layout.
            struct.pack_into("<6I", firmware, 72, 16, 4, 64 * 1024 * 1024, 0, 4, 15)
            (content / "firmware/intflash.bin").write_bytes(firmware)
            (content / "firmware/retro-go-debug.elf").write_bytes(b"\x7fELF-test")
            (content / "flash/frogfs.bin").write_bytes(b"FROG")
            (content / "flash/littlefs.bin").write_bytes(b"LFS!")
            metadata = {
                "variant": "flash",
                "debugSymbols": "firmware/retro-go-debug.elf",
                "littlefsBlockSize": 4096,
                "files": [
                    "firmware/intflash.bin",
                    "firmware/retro-go-debug.elf",
                    "flash/frogfs.bin",
                    "flash/littlefs.bin",
                ],
                "firmware": {
                    "version": "test",
                    "providesAbi": {"version": 2, "size": 908},
                    "superblock": self.declaration,
                },
                "layout": {
                    "extflashBytes": 64 * 1024 * 1024,
                    "frogfsOffset": 16,
                    "frogfsBytes": 4,
                    "littlefsOffset": 64 * 1024 * 1024 - 4,
                    "littlefsBytes": 4,
                },
            }
            (content / ".gwprov-firmware.json").write_text(json.dumps(metadata))
            bootloader = Path(tmp) / "bootloader.bin"
            bootloader.write_bytes(struct.pack("<II", 0x20020000, 0x08000009) + b"\x00\xbf")
            profile = Path(tmp) / "profile"
            create_profile(profile, content=content, bootloader_file=bootloader)
            mapping = yaml.safe_load((profile / "flash-map.yaml").read_text())
            frog = next(item for item in mapping["bank0"] if item["name"].endswith("FrogFS"))
            self.assertEqual(frog["offset"], 16)
            self.assertEqual((profile / frog["file"]).read_bytes(), b"FROG")
            with (profile / "extflash.bin").open("rb") as stream:
                stream.seek(16)
                self.assertEqual(stream.read(4), b"FROG")
                stream.seek(64 * 1024 * 1024 - 4)
                self.assertEqual(stream.read(4), b"LFS!")

    def test_sd_profile_keeps_staged_content_when_size_is_reapplied(self):
        from gwprov.cli.main import _configure_profile_sdcard
        from gwprov.common.sdcard import QemuSDCardManager

        with tempfile.TemporaryDirectory() as tmp:
            content = Path(tmp) / "content"
            (content / "firmware").mkdir(parents=True)
            (content / "sd/fonts").mkdir(parents=True)
            (content / "firmware/intflash.bin").write_bytes(self.firmware())
            (content / "sd/fonts/test-font.bin").write_bytes(b"font-data")
            (content / ".gwprov-firmware.json").write_text(
                json.dumps(
                    {
                        "variant": "sd",
                        "files": ["firmware/intflash.bin", "sd/fonts/test-font.bin"],
                        "firmware": {
                            "version": "test",
                            "providesAbi": {"version": 2, "size": 908},
                            "superblock": self.declaration,
                        },
                    }
                )
            )
            bootloader = Path(tmp) / "bootloader.bin"
            bootloader.write_bytes(struct.pack("<II", 0x20020000, 0x08000009) + b"\x00\xbf")
            profile = Path(tmp) / "profile"
            create_profile(
                profile,
                content=content,
                sd_size_mib=4,
                extflash_mib=64,
                bootloader_file=bootloader,
            )

            # The CLI reapplies --sdcard-size after profile creation. This must
            # be idempotent when create_profile already populated that image.
            _configure_profile_sdcard(profile, size_mib=4)

            copied = Path(tmp) / "copied-font.bin"
            import subprocess

            subprocess.run(
                [
                    "mcopy",
                    "-i",
                    f"{profile / 'sdcard.img'}@@1M",
                    "::fonts/test-font.bin",
                    str(copied),
                ],
                check=True,
            )
            self.assertEqual(copied.read_bytes(), b"font-data")
            listing = QemuSDCardManager(str(profile / "sdcard.img")).listing("/fonts")
            self.assertIn("test-font.bin", listing)


if __name__ == "__main__":
    unittest.main()
