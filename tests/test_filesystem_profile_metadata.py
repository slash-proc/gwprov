"""Profile filesystem metadata remains compatible with legacy manifests."""

import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from gwprov.filesystem import _profile_region


class ProfileRegionMetadata(unittest.TestCase):
    def test_legacy_firmware_string_uses_default_littlefs_block_size(self):
        self.assertEqual(self._region_for_firmware("legacy-build"), 4096)

    def test_firmware_object_keeps_declared_littlefs_block_size(self):
        self.assertEqual(self._region_for_firmware({"littlefsBlockSize": 8192}), 8192)

    def _region_for_firmware(self, firmware):
        root = Path(tempfile.mkdtemp(prefix="profile-region-", dir=os.environ.get("TMPDIR", Path(__file__).parent)))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        (root / "extflash.bin").write_bytes(bytes(16 * 1024))
        (root / "profile.toml").write_text(
            "version = 1\ndisplay_name = 'test'\n[flash]\n"
            "bank1 = 'bank1.bin'\nbank2 = 'bank2.bin'\nextflash = 'extflash.bin'\n",
            encoding="utf-8",
        )
        (root / "provision.json").write_text(
            json.dumps(
                {
                    "firmware": firmware,
                    "layout": {
                        "littlefsBytes": 4096,
                        "frogfsOffset": 0,
                        "frogfsBytes": 4096,
                    },
                }
            ),
            encoding="utf-8",
        )
        _profile, _image, _kind, _offset, layout = _profile_region(str(root), "flash/ext")
        return layout["block_size"]


if __name__ == "__main__":
    unittest.main()
