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
            path = Path(tmp)/'archive.zip'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('folder/',b'')
                archive.writestr('folder/Actual Game.gba',b'game')
            self.assertEqual(read_input(path,extensions={'.gba'}),('Actual Game.gba',b'game'))

    def test_multiple_files_are_never_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'archive.zip'
            with zipfile.ZipFile(path,'w') as archive:
                archive.writestr('game.gba',b'game')
                archive.writestr('readme.txt',b'notes')
            with self.assertRaisesRegex(ValueError,'exactly one'):
                read_input(path,extensions={'.gba'})
            self.assertEqual(read_input(path,extensions={'.zip'})[1],path.read_bytes())

    def test_unpacked_size_is_checked_before_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'archive.zip'
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr('game.gba',b'\0'*10000)
            with self.assertRaisesRegex(ValueError,'unpacked input exceeds'):
                read_input(path,extensions={'.gba'},max_bytes=100)

    def test_zip_extension_and_compression_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'archive.zip'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('notes.txt',b'notes')
            with self.assertRaisesRegex(ValueError,'expected extension'):
                read_input(path,extensions={'.gba'})
            with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_BZIP2) as archive:
                archive.writestr('game.gba',b'game')
            with self.assertRaisesRegex(ValueError,'stored and deflate'):
                read_input(path,extensions={'.gba'})


class Profile(unittest.TestCase):
    declaration={'magic':'GWLB','version':2,'structSize':36}

    def firmware(self):
        return (struct.pack('<II',0x20020000,0x08100101)+b'\0'*56+
                struct.pack('<4sHH7I',b'GWLB',2,36,0,0,0,0,0,0,0))

    def test_layout_crc_matches_firmware_contract(self):
        output=patch_layout(self.firmware(),self.declaration,frogfs_length=1234,
                            extflash_size=64*1024*1024,littlefs_length=2*1024*1024)
        self.assertEqual(struct.unpack_from('<6I',output,72),
                         (0,1234,64*1024*1024,0,2*1024*1024,15))
        self.assertEqual(struct.unpack_from('<I',output,96)[0],zlib.crc32(output[64:96]))

    def test_ambiguous_superblock_is_rejected(self):
        firmware=self.firmware()
        with self.assertRaisesRegex(ValueError,'found 2'):
            patch_layout(firmware+firmware[64:],self.declaration,frogfs_length=1,
                         extflash_size=64*1024*1024,littlefs_length=2*1024*1024)

    def test_real_filesystems_profile_and_source_preservation(self):
        from littlefs import LittleFS
        with tempfile.TemporaryDirectory() as tmp:
            content=Path(tmp)/'content'
            (content/'firmware').mkdir(parents=True)
            (content/'firmware/intflash.bin').write_bytes(self.firmware())
            (content/'flash/frogfs/roms/gba').mkdir(parents=True)
            (content/'flash/frogfs/roms/gba/game.gba').write_bytes(b'game')
            (content/'flash/littlefs/cores').mkdir(parents=True)
            (content/'flash/littlefs/cores/gba.bin').write_bytes(b'core')
            (content/'.gwprov-firmware.json').write_text(json.dumps({
                'variant':'flash','littlefsBlockSize':4096,
                'firmware':{'providesAbi':{'version':2,'size':908},'superblock':self.declaration}}))
            before={str(p.relative_to(content)):p.read_bytes() for p in content.rglob('*') if p.is_file()}
            profile=Path(tmp)/'profile'
            bootloader=Path(tmp)/'bootloader.bin'
            bootloader.write_bytes(struct.pack('<II',0x20020000,0x08000009)+b'\x00\xbf')
            report=create_profile(profile,content=content,littlefs_mib=1,bootloader_file=bootloader)
            self.assertEqual((profile/'extflash.bin').stat().st_size,64*1024*1024)
            self.assertEqual((profile/'extflash.bin').read_bytes()[:4],b'FROG')
            self.assertEqual((profile/'bank2.bin').stat().st_size,256*1024)
            self.assertEqual(before,{str(p.relative_to(content)):p.read_bytes() for p in content.rglob('*') if p.is_file()})
            with (profile/'extflash.bin').open('rb') as image:
                image.seek(report['layout']['littlefsOffset']);reverse=image.read()
            linear=b''.join(reverse[i:i+4096] for i in range(len(reverse)-4096,-1,-4096))
            fs=LittleFS(block_size=4096,block_count=256,read_size=256,prog_size=256,cache_size=256,
                        lookahead_size=16,mount=False)
            fs.context.buffer=bytearray(linear)
            fs.mount()
            with fs.open('/cores/gba.bin','rb') as f:self.assertEqual(f.read(),b'core')
            fs.unmount()
            with self.assertRaisesRegex(ValueError,'already exists'):create_profile(profile,content=content)


if __name__=='__main__':unittest.main()
