from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gwprov.gwemu_manager import start_instance
from gwprov.launch import launch_profile


class Launch(unittest.TestCase):
    def test_sd_image_format_comes_from_signature(self):
        for signature, expected in ((b'QFI\xfb', 'qcow2'), (b'FAT ', 'raw')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ('bank1.bin', 'bank2.bin', 'extflash.bin'):
                    (root / name).write_bytes(b'image')
                sd = root / 'card.img'
                sd.write_bytes(signature + b'contents')
                (root / 'profile.toml').write_text(
                    "version = 1\ndisplay_name = 'test'\n"
                    "[flash]\nbank1 = 'bank1.bin'\nbank2 = 'bank2.bin'\n"
                    "extflash = 'extflash.bin'\n[sd]\nmode = 'bundled'\n"
                    "image = 'card.img'\n",
                    encoding='utf-8',
                )
                with patch('gwprov.launch.subprocess.Popen') as popen:
                    popen.return_value.wait.return_value = 0
                    self.assertEqual(launch_profile(root, headless=True), 0)
                command = popen.call_args.args[0]
                drive = command[command.index('-drive') + 1]
                self.assertIn(f'format={expected}', drive)

    def test_default_qmp_socket_is_short_for_macos_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('bank1.bin', 'bank2.bin', 'extflash.bin'):
                (root / name).write_bytes(b'image')
            (root / 'profile.toml').write_text(
                "version = 1\ndisplay_name = 'test'\n"
                "[flash]\nbank1 = 'bank1.bin'\nbank2 = 'bank2.bin'\n"
                "extflash = 'extflash.bin'\n",
                encoding='utf-8',
            )
            with patch('gwprov.gwemu_manager.instances', return_value=[]), \
                    patch('gwprov.launch.launch_profile', return_value=0) as launch:
                self.assertEqual(start_instance(str(root), gdb_port=4321), 0)
            qmp_socket = launch.call_args.kwargs['qmp_socket']
            self.assertLess(len(qmp_socket.encode()), 104)


if __name__ == '__main__':
    unittest.main()
