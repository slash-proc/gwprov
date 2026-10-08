"""Launch a provisioned instance directly, preserving its firmware and media state."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

from .profiles import DeviceProfile


def launch_profile(directory: str | Path, *, headless: bool = False, audio: bool = False,
                   timeline: str | None = None, gdb_port: int | None = None,
                   qmp_socket: str | None = None) -> int:
    profile = DeviceProfile.load(directory)
    for path in (profile.bank1, profile.bank2, profile.extflash):
        if not path.is_file():raise ValueError(f'profile image is missing: {path}')
    cmd = ['gwemu', '-machine', 'gnw-h7b0']
    for prop,path in [('bank1-image',profile.bank1),('bank2-image',profile.bank2),
                      ('extflash-image',profile.extflash)]:
        cmd += ['-global', f'gnw-h7b0-soc.{prop}={path}']
    rdp = profile.root / 'rdp-state.bin'
    if not rdp.exists():rdp = profile.root / 'rdp.bin'
    cmd += ['-global',f'gnw-h7b0-soc.rdp-image={rdp}']
    cmd += ['-display', 'none' if headless else 'gwemu', '-audiodev',
            'sdl3,id=snd0' if audio else 'none,id=snd0',
            '-global', 'gnw-h7b0-sai1.audiodev=snd0']
    if profile.resolved_sd:
        cmd += ['-drive',f'if=sd,format=raw,file={profile.resolved_sd}']
    if gdb_port is not None:cmd += ['-gdb',f'tcp:127.0.0.1:{gdb_port}']
    if qmp_socket:cmd += ['-qmp',f'unix:{Path(qmp_socket).resolve()},server=on,wait=off']
    config = profile.root / 'gwemu.toml'
    if not config.exists():config.write_text('[general]\nshow_welcome = false\n')
    if not headless:
        cmd += ['-config_path',str(config)]
    env = dict(os.environ)
    env['XDG_DATA_HOME'] = str(profile.root/'runtime')
    env['XDG_CONFIG_HOME'] = str(profile.root/'runtime/config')
    if timeline:env['GNW_TIMELINE'] = str(Path(timeline).expanduser().resolve())
    with (profile.root/'gwemu.log').open('w') as log:
        process = subprocess.Popen(cmd,cwd=profile.root,env=env,stderr=log)
        try:return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
            return 130
