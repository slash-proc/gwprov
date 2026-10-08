"""Read-only, deterministic filesystem tables and hashes for provisioned media."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import struct
import zlib


def digest(stream, size=None):
    h = hashlib.sha256()
    while size is None or size:
        chunk = stream.read(min(size, 1024*1024) if size is not None else 1024*1024)
        if not chunk:
            if size: raise ValueError('Truncated image or filesystem data')
            break
        h.update(chunk)
        if size is not None: size -= len(chunk)
    return h.hexdigest()


def region_hash(image, offset, size):
    if offset < 0 or size < 0 or offset+size > image.stat().st_size:
        raise ValueError('Filesystem region exceeds image')
    with image.open('rb') as stream:
        stream.seek(offset)
        return digest(stream, size)


def table(kind, offset, size, entries, raw_hash):
    entries.sort(key=lambda item: item['path'])
    canonical = json.dumps(entries, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return dict(filesystem=kind, offset=offset, bytes=size, sha256=raw_hash,
                tableSha256=hashlib.sha256(canonical.encode()).hexdigest(), entries=entries)


def frogfs(image, offset=0):
    with image.open('rb') as stream:
        stream.seek(offset)
        magic, major, minor, count, size=struct.unpack('<IBBHI',stream.read(12))
        if magic != 0x474f5246 or major != 1: raise ValueError('Unsupported FrogFS header')
        if size < 16+count*8: raise ValueError('Invalid FrogFS size')
        raw_hash=region_hash(image,offset,size)
        stream.seek(offset+12)
        offsets=[struct.unpack('<II',stream.read(8))[1] for i in range(count)]
        nodes={}
        for pos in offsets:
            if pos+8 > size: raise ValueError('FrogFS entry exceeds region')
            stream.seek(offset+pos)
            parent, flags, length, options=struct.unpack('<IHBB',stream.read(8))
            file=(flags & 0xff00)==0xff00
            header_size=20 if file and flags & 0xff else 16 if file else 8+4*flags
            if pos+header_size+length > size: raise ValueError('FrogFS name exceeds region')
            stream.seek(offset+pos+header_size)
            name=stream.read(length).decode('utf-8')
            nodes[pos]=(parent,name,file,flags)
        def path_for(pos, seen=None):
            seen=set() if seen is None else seen
            if pos in seen: raise ValueError('Cyclic FrogFS parent table')
            seen.add(pos)
            parent,name,_,_=nodes[pos]
            return '/'.join(filter(None,[path_for(parent,seen) if parent else '',name]))
        entries=[]
        for pos,(parent,name,file,flags) in nodes.items():
            path=path_for(pos)
            if not path: continue
            row={'path':path,'type':'file' if file else 'directory'}
            if file:
                stream.seek(offset+pos+8)
                start,length=struct.unpack('<II',stream.read(8))
                if start+length > size: raise ValueError('FrogFS file exceeds region')
                stream.seek(offset+start)
                row.update(bytes=length,sha256=digest(stream,length),
                           hashKind='stored',compression=flags & 0xff)
            entries.append(row)
    return table('frogfs',offset,size,entries,raw_hash)


def content_entry(path, stream, size):
    row = dict(path=path.lstrip('/'), type='file', bytes=size,
               sha256=digest(stream), hashKind='content')
    if row['path'].lower() == 'data/install' and size == 80:
        stream.seek(0)
        data = bytearray(stream.read(80))
        if data[:5] == b'RGIN\x01':
            crc = struct.unpack_from('<I', data, 76)[0]
            data[76:80] = bytes(4)
            if zlib.crc32(data) == crc:
                row['installedAt'] = struct.unpack_from('<I', data, 72)[0]
                data[72:76] = bytes(4)
                row['comparisonSha256'] = hashlib.sha256(data).hexdigest()
                row['normalization'] = 'rgin-v1-installed-at-and-crc'
    return row


def littlefs(image, offset, size, block_size=4096):
    from littlefs import LittleFS
    # Address reversal matches Retro-Go's flash allocator. Never auto-format on mount error.
    class ReadOnlyContext:
        def __init__(self, stream): self.stream=stream
        def read(self,cfg,block,off,count):
            self.stream.seek(offset+size-(block+1)*block_size+off)
            return bytearray(self.stream.read(count))
        def prog(self,*args): raise ValueError('Inventory is read-only')
        def erase(self,*args): raise ValueError('Inventory is read-only')
        def sync(self,*args): return 0
    if size % block_size: raise ValueError('LittleFS region is not block aligned')
    raw_hash=region_hash(image,offset,size)
    with image.open('rb') as stream:
        fs=LittleFS(context=ReadOnlyContext(stream),mount=False,block_size=block_size,
                    block_count=size//block_size)
        fs.mount()
        try:
            entries=[]
            for root,dirs,files in fs.walk('/'):
                for name in dirs:
                    entries.append(dict(path=(root.rstrip('/')+'/'+name).lstrip('/'),type='directory'))
                for name in files:
                    path=root.rstrip('/')+'/'+name
                    with fs.open(path,'rb') as file:
                        entries.append(content_entry(path, file, fs.stat(path).size))
        finally: fs.unmount()
    return table('littlefs',offset,size,entries,raw_hash)


def fatfs(image, offset=0):
    from pyfatfs.PyFatFS import PyFatFS
    with image.open('rb') as stream:
        stream.seek(offset); boot=stream.read(512)
    sectors=struct.unpack_from('<H',boot,19)[0] or struct.unpack_from('<I',boot,32)[0]
    size=sectors*struct.unpack_from('<H',boot,11)[0]
    raw_hash=region_hash(image,offset,size)
    with PyFatFS(str(image),offset=offset,read_only=True) as fs:
        entries=[]
        for path,info in fs.walk.info(namespaces=['details']):
            if info.is_dir: entries.append(dict(path=path.lstrip('/'),type='directory'))
            else:
                with fs.openbin(path,'r') as file:
                    entries.append(content_entry(path, file, info.size))
    return table('fatfs',offset,size,entries,raw_hash)


def inspect_profile(directory, shared_sd_root=None):
    from .profiles import DeviceProfile
    p=DeviceProfile.load(directory,shared_sd_root=shared_sd_root)
    result={'schemaVersion':1,'images':{},'filesystems':[]}
    for name,file in [('bank1',p.bank1),('bank2',p.bank2),('extflash',p.extflash),
                      ('rdp',p.root/'rdp-state.bin'),('sd',p.resolved_sd)]:
        if file and file.is_file():
            with file.open('rb') as stream:
                result['images'][name]={'bytes':file.stat().st_size,'sha256':digest(stream)}
    layout=None
    for bank in (p.bank2,p.bank1):
        data=bank.read_bytes()
        for pos in range(0,len(data)-35,4):
            if data[pos:pos+8]==b'GWLB\x02\x00\x24\x00' and zlib.crc32(data[pos:pos+32])==struct.unpack_from('<I',data,pos+32)[0]:
                layout=struct.unpack_from('<6I',data,pos+8);break
        if layout:break
    if layout:
        frog_start,frog_size,end,_,lfs_size,flags=layout
        if frog_size: result['filesystems'].append(frogfs(p.extflash,frog_start))
        if lfs_size:
            metadata=json.loads((p.root/'provision.json').read_text()) if (p.root/'provision.json').exists() else {}
            firmware=metadata.get('firmware',{})
            block=firmware.get('littlefsBlockSize',4096) if isinstance(firmware,dict) else 4096
            result['filesystems'].append(littlefs(p.extflash,end-lfs_size,lfs_size,block))
    if p.resolved_sd:
        with p.resolved_sd.open('rb') as stream: mbr=stream.read(512)
        partitions=[]
        if mbr[510:512]==b'\x55\xaa':
            for i in range(4):
                entry=mbr[446+i*16:462+i*16]
                if entry[4] in (1,4,6,11,12,14):partitions.append(struct.unpack_from('<I',entry,8)[0]*512)
        for offset in partitions or [0]: result['filesystems'].append(fatfs(p.resolved_sd,offset))
    return result


def compare(expected,actual,mode='contents'):
    def selected(value):
        if mode=='image':return {'images':value['images'], 'regions':[{key:row[key] for key in ('filesystem','offset','bytes','sha256')} for row in value['filesystems']]}
        rows=[]
        for row in value['filesystems']:
            entries=[]
            for entry in row['entries']:
                entry=dict(entry)
                if entry.get('normalization') == 'rgin-v1-installed-at-and-crc':
                    entry['sha256']=entry.pop('comparisonSha256')
                    entry.pop('installedAt')
                entries.append(entry)
            rows.append({'filesystem':row['filesystem'],'entries':entries})
        return rows
    a,b=selected(expected),selected(actual)
    return {'equal':a==b,'mode':mode,'expected':a if a!=b else None,'actual':b if a!=b else None}
