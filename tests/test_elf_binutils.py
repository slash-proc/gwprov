import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from gwprov.elf_binutils import select_elf_tool

class ElfToolTests(unittest.TestCase):
    def selected(self, machine, available):
        with tempfile.TemporaryDirectory(dir='build') as folder:
            image=Path(folder)/'image';image.write_bytes(b'fixture')
            tool=Path(folder)/'tool';tool.write_bytes(b'fixture')
            def which(name):return str(tool) if name in available else None
            with patch('gwprov.elf_binutils.ELFFile',return_value={'e_machine':machine}),patch('gwprov.elf_binutils.shutil.which',side_effect=which),patch('gwprov.elf_binutils._version',return_value='test-version'):
                return select_elf_tool(image)
    def test_host_never_selects_arm_tool(self):
        result=self.selected('EM_X86_64',{'arm-none-eabi-objdump','objdump'})
        self.assertEqual(result['machine'],'EM_X86_64')
        self.assertEqual(result['version'],'test-version')
    def test_arm_selects_cross_tool(self):
        result=self.selected('EM_ARM',{'arm-none-eabi-objdump'})
        self.assertEqual(result['machine'],'EM_ARM')
    def test_unknown_machine_fails(self):
        with self.assertRaisesRegex(ValueError,'unsupported ELF machine'):
            self.selected('EM_FAKE',{'objdump'})
    def test_missing_tool_fails(self):
        with self.assertRaisesRegex(FileNotFoundError,'no objdump'):
            self.selected('EM_ARM',set())

if __name__=='__main__':unittest.main()
