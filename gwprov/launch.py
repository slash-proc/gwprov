"""Launch a provisioned instance directly, preserving its firmware and media state."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from .profiles import DeviceProfile

BANK_SIZE = 256 * 1024


def gwemu_executable(binary=None):
    """Keep PATH defaults; explicit binaries must be existing executable files."""
    if binary is None:
        return "gwemu"
    path = Path(binary).expanduser().resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError(f"GWemu binary is not executable: {path}")
    return str(path)


def timing_launch_args(*, timing_mode="default", icount=None, rtc_epoch=None):
    """Build explicit timing controls; baseline counts instructions, not silicon cycles."""
    if timing_mode not in ("default", "baseline", "experimental-m7"):
        raise ValueError("timing mode must be default, baseline or experimental-m7")
    if icount is not None and (isinstance(icount, bool) or not isinstance(icount, int) or not 0 <= icount <= 10):
        raise ValueError("icount shift must be an integer from 0 to 10")
    if timing_mode in ("baseline", "experimental-m7") and icount not in (None, 0):
        raise ValueError("modeled timing requires --icount 0 (or omit --icount)")
    args = []
    if timing_mode in ("baseline", "experimental-m7"):
        icount = 0
        args += ["-global", "gnw-h7b0-soc.timing-mode=on"]
    if timing_mode == "experimental-m7":
        args += ["-global", "cortex-m7-arm-cpu.x-gnw-m7-cycle-model=on"]
    if icount is not None:
        args += ["-icount", f"shift={icount},align=off,sleep=off"]
    if rtc_epoch is not None:
        if isinstance(rtc_epoch, bool) or not isinstance(rtc_epoch, int) or rtc_epoch < 0:
            raise ValueError("RTC epoch must be a nonnegative Unix timestamp")
        args += ["-global", f"gnw-h7b0-rtc.initial-epoch={rtc_epoch}"]
    return args


def image_launch_spec(image: dict[str, str], *, headless: bool = False,
                      audio: bool = False, timeline: str | None = None,
                      record_timeline: str | None = None,
                      gdb_port: int | None = None, icount: int | None = None,
                      timing_mode: str = "default", rtc_epoch: int | None = None,
                      keep_temp: bool = False, gwemu_bin: str | None = None) -> tuple[list[str], Path, dict[str, str], Path, Path | None]:
    """Stage a raw image set and build a daemon-managed GWemu launch."""
    run_root = Path(__file__).resolve().parent.parent / "dev-local" / "runs"
    run_root.mkdir(parents=True, exist_ok=True)
    cwd = Path(tempfile.mkdtemp(prefix=f"gwemu-{os.getpid()}-", dir=run_root))
    try:
        for key in ("bank1", "bank2"):
            source = Path(image.get(key, "")).expanduser() if image.get(key) else None
            payload = source.read_bytes() if source and source.is_file() else b""
            if len(payload) > BANK_SIZE:
                raise ValueError(f"{source} is {len(payload)}B, larger than a bank")
            (cwd / f"qemu_{key}.bin").write_bytes(payload.ljust(BANK_SIZE, b"\0"))
        cmd = [gwemu_executable(gwemu_bin), "-machine", "gnw-h7b0",
               "-global", "gnw-h7b0-soc.bank1-image=qemu_bank1.bin",
               "-global", "gnw-h7b0-soc.bank2-image=qemu_bank2.bin"]
        sd_format = "raw"
        for key, filename, prop in (("extflash", "extflash.bin", "extflash-image"),
                                    ("sdcard", "sdcard.img", None)):
            source_text = image.get(key, "")
            if not source_text:
                continue
            source = Path(source_text).expanduser()
            if not source.is_file():
                raise ValueError(f"image file is missing: {source}")
            if key == "sdcard":
                sd_format = _sd_image_format(source)
            shutil.copyfile(source, cwd / filename)
            if prop:
                cmd += ["-global", f"gnw-h7b0-soc.{prop}={filename}"]
        cmd += ["-audiodev", "sdl3,id=snd0" if audio else "none,id=snd0",
                "-global", "gnw-h7b0-sai1.audiodev=snd0"]
        if (cwd / "sdcard.img").is_file():
            cmd += ["-drive", f"if=sd,format={sd_format},file=sdcard.img"]
        cmd += ["-display", "none" if headless else "gwemu",
                "-monitor", "none", "-qmp", "stdio"]
        if gdb_port is not None:
            cmd += ["-gdb", f"tcp:127.0.0.1:{gdb_port}"]
        cmd += timing_launch_args(timing_mode=timing_mode, icount=icount, rtc_epoch=rtc_epoch)
        env = dict(os.environ)
        from .timeline_launch import configure_timeline
        configure_timeline(env, timeline=timeline, record_timeline=record_timeline,
                           headless=headless)
        cleanup = None if keep_temp else cwd
        return cmd, cwd, env, cwd / "gwemu.log", cleanup
    except Exception:
        shutil.rmtree(cwd, ignore_errors=True)
        raise


def _sd_image_format(path: Path) -> str:
    """Return the block format for an SD image from its file signature."""
    with path.open('rb') as image:
        return 'qcow2' if image.read(4) == b'QFI\xfb' else 'raw'


def profile_launch_spec(directory: str | Path, *, headless: bool = False,
                        audio: bool = False, timeline: str | None = None,
                        record_timeline: str | None = None,
                        gdb_port: int | None = None,
                        qmp_socket: str | None = None,
                        qmp_stdio: bool = False,
                        start_halted: bool = False,
                        timing_mode: str = "default", icount: int | None = None,
                        rtc_epoch: int | None = None, gwemu_bin: str | None = None,
                        shared_sd_root: str | Path | None = None) -> tuple[list[str], Path, dict[str, str], Path]:
    """Build the GWemu command and environment for a provisioned profile.

    The daemon uses ``qmp_stdio`` so QEMU has no listening QMP endpoint. The
    older foreground launcher can continue to use a local QMP socket.
    """
    profile = DeviceProfile.load(directory, shared_sd_root=shared_sd_root)
    for path in (profile.bank1, profile.bank2, profile.extflash):
        if not path.is_file():
            raise ValueError(f'profile image is missing: {path}')
    cmd = [gwemu_executable(gwemu_bin), '-machine', 'gnw-h7b0']
    for prop, path in [('bank1-image', profile.bank1), ('bank2-image', profile.bank2),
                       ('extflash-image', profile.extflash)]:
        cmd += ['-global', f'gnw-h7b0-soc.{prop}={path}']
    cmd += ['-display', 'none' if headless else 'gwemu', '-audiodev',
            'sdl3,id=snd0' if audio else 'none,id=snd0',
            '-global', 'gnw-h7b0-sai1.audiodev=snd0']
    if profile.resolved_sd:
        if not profile.resolved_sd.is_file():
            raise ValueError(f'profile SD image is missing: {profile.resolved_sd}')
        image_format = _sd_image_format(profile.resolved_sd)
        cmd += ['-drive', f'if=sd,format={image_format},file={profile.resolved_sd}']
    if gdb_port is not None:
        cmd += ['-gdb', f'tcp:127.0.0.1:{gdb_port}']
    cmd += timing_launch_args(timing_mode=timing_mode, icount=icount, rtc_epoch=rtc_epoch)
    if start_halted:
        cmd += ['-S']
    if qmp_stdio:
        cmd += ['-monitor', 'none', '-qmp', 'stdio']
    elif qmp_socket:
        cmd += ['-qmp', f'unix:{Path(qmp_socket).resolve()},server=on,wait=off']
    config = profile.root / 'gwemu.toml'
    if not config.exists():
        config.write_text('[general]\nshow_welcome = false\n')
    if not headless:
        cmd += ['-config_path', str(config)]
    env = dict(os.environ)
    env['XDG_DATA_HOME'] = str(profile.root / 'runtime')
    env['XDG_CONFIG_HOME'] = str(profile.root / 'runtime/config')
    from .timeline_launch import configure_timeline
    configure_timeline(env, timeline=timeline, record_timeline=record_timeline,
                       headless=headless)
    return cmd, profile.root, env, profile.root / 'gwemu.log'


def launch_profile(directory: str | Path, *, headless: bool = False, audio: bool = False,
                   timeline: str | None = None, record_timeline: str | None = None, gdb_port: int | None = None,
                   qmp_socket: str | None = None) -> int:
    cmd, cwd, env, log_path = profile_launch_spec(
        directory, headless=headless, audio=audio, timeline=timeline,
        record_timeline=record_timeline, gdb_port=gdb_port, qmp_socket=qmp_socket)
    with log_path.open('w') as log:
        process = subprocess.Popen(cmd, cwd=cwd, env=env, stderr=log)
        try:return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()
            return 130
