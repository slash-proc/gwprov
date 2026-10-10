"""
One target abstraction for every tier.

This replaces what used to be four modules -- runner.py, qemu_runner.py,
hardware_runner.py and retrogo_runner.py -- which duplicated launch, symbol and
SD handling three times over and left the retro-go path unable to run on
hardware at all.

Two ideas make one module enough:

  * WHAT boots is an Image: bank1/bank2/extflash/sdcard. The bare-metal CPU
    harness and the retro-go firmware differ only in which bytes those are.
  * WHERE it boots is a Target: GwemuTarget (GDBBackend) or HardwareTarget
    (OpenOCDBackend). Past `.open()` they behave identically, which is exactly
    why one test script can drive both.

SD card access goes through scripts/common/sdcard.py, which already models the
three real paths: mcopy into an image (gwemu), sd_write_file over the probe
(device, incremental), and a plain copy to a mounted block device (device, bulk
-- repackaging DOS ROMs).
"""

import atexit
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass

from gnwmanager.gnw import GnW
from gnwmanager.ocdbackend.gdb_backend import GDBBackend
from ..backends import AutoOpenOCDBackend

from .sdcard import (HardwarePhysicalSDCardManager, HardwareProbeSDCardManager,
                     QemuSDCardManager)
from .timeline import (ShadowWordPlayer, TimelineError, parse,
                       quit_time as timeline_quit)

# gwemu key map (gnw-input.ini), per the firmware's docs/gwemu.md.
KEYS = {
    "A": "x", "B": "z", "GAME": "g", "TIME": "t",
    "PAUSE": "esc", "POWER": "p", "START": "ret", "SELECT": "shift_r",
    "UP": "up", "DOWN": "down", "LEFT": "left", "RIGHT": "right",
}
HOLD_MS = 600   # 200 is too short for the launcher to register; do not lower

BANK_SIZE = 256 * 1024   # gwemu refuses anything else

# Run scratch lives in the PROJECT, never /tmp. Each gwemu run copies the SD
# image and extflash (100s of MB) into its own directory; on a tmpfs box that
# is RAM, and leaking a few dozen runs fills it. Always deleted -- pass
# keep_temp / --keep-temp only when you need the artifacts to look at.
RUN_ROOT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "dev-local", "runs")


def _make_run_dir():
    os.makedirs(RUN_ROOT, exist_ok=True)
    d = os.path.join(os.path.abspath(RUN_ROOT), f"gwemu-{os.getpid()}-{id(object())}")
    os.makedirs(d, exist_ok=True)
    return d


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


class _PipeSocket:
    """Socket-shaped adapter for QEMU's GDB stdio chardev."""

    def __init__(self, proc):
        self.proc = proc

    def sendall(self, data):
        self.proc.stdin.write(data)
        self.proc.stdin.flush()

    def makefile(self, mode, buffering=-1):
        return self.proc.stdout

    def close(self):
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except OSError:
                pass


class GDBPipeBackend(GDBBackend):
    """GDB remote protocol over pipes, for sandboxes that deny local sockets."""

    def __init__(self, proc):
        super().__init__()
        self.proc = proc

    def open(self):
        self._socket = _PipeSocket(self.proc)
        self._sock_file = self._socket.makefile("rb", buffering=8192)
        try:
            self._send_command(b"qSupported:multiprocess+;xmlRegisters=i386;qRelocInsn+")
        except Exception:
            # The normal backend deliberately tolerates a stub without
            # qSupported. Pipe transport has the same compatibility rule.
            pass
        self.resume()
        return self


@dataclass
class Image:
    """The bytes to boot. bank1/bank2 are internal flash; the rest optional."""
    bank1: str
    bank2: str = ""
    extflash: str = ""
    sdcard: str = ""
    elf: str = ""            # for symbol lookup
    intflash_bank: int = 1   # which bank the firmware was LINKED for
    intflash: str = ""       # HARDWARE image; see hw_primary

    @property
    def hw_primary(self):
        """The image to write to a real device's internal flash.

        NOT the same file as `primary`. The build emits
        gw_retro_go_intflash.bin for silicon and qemu_bank{1,2}.bin for gwemu;
        the qemu banks are PADDED OUT to a full 256 KB bank so the emulator can
        map them. Flashing the padded twin writes 35 KB of padding over the
        rest of the bank, and is not what a working hardware flash does.
        """
        return self.intflash or self.primary

    @property
    def primary(self):
        """The bank image that actually carries the firmware."""
        return self.bank2 if self.intflash_bank == 2 else self.bank1


def bare_metal_image(elf="test_core.elf", bin_="test_core_intflash.bin"):
    """Tier 1: our own bare-metal harness, no SD, no extflash needed."""
    return Image(bank1=bin_, elf=elf, intflash_bank=1)


def timeline_quit_time(path):
    """Seconds until a timeline's `quit` event, or None if it has none.

    A test that polls the target for longer than this outlives gwemu: the
    process exits on `quit` and the next backend read fails with "Connection
    closed by remote", which reads as a target fault rather than a harness
    bug. Callers clamp their waits with this instead of hardcoding a duration
    that silently goes stale when the timeline changes.
    """
    try:
        return timeline_quit(parse(path))
    except (OSError, TimelineError):
        return None


class QMP:
    """Minimal QMP client. Used for screendump; input goes via timelines."""


    def __init__(self, port, timeout=20.0):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("localhost", port), 2.0)
                break
            except OSError as exc:
                last = exc
                time.sleep(0.2)
        else:
            raise RuntimeError(f"could not reach QMP on {port}: {last}")
        self.f = self.sock.makefile("rw", encoding="utf-8", newline="\n")
        self._read()
        self._cmd("qmp_capabilities")

    def _read(self):
        while True:
            line = self.f.readline()
            if not line:
                raise RuntimeError("QMP closed")
            msg = json.loads(line)
            if "event" in msg:
                continue
            return msg

    def _cmd(self, execute, **args):
        payload = {"execute": execute}
        if args:
            payload["arguments"] = args
        self.f.write(json.dumps(payload) + "\n")
        self.f.flush()
        return self._read()

    def send_key(self, name, hold_ms=HOLD_MS):
        return self._cmd("send-key",
                         keys=[{"type": "qcode", "data": KEYS[name.upper()]}],
                         **{"hold-time": hold_ms})

    def screendump(self, path):
        return self._cmd("screendump", filename=os.path.abspath(path))

    def close(self):
        try:
            self.f.close()
            self.sock.close()
        except Exception:
            pass


class _SerialBackend:
    """Serialise every backend call behind one lock.

    A hardware timeline is delivered from its own thread while the harness
    keeps polling memory on the main one, and both go down a SINGLE OpenOCD
    socket. Interleaving two request/response exchanges on it desynchronises
    the link and the failure looks like a corrupted read, not a race. Every
    attribute access is wrapped, so nothing has to remember to take the lock.
    """

    def __init__(self, inner, lock):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_lock", lock)

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr
        lock = self._lock

        def locked(*a, **kw):
            with lock:
                return attr(*a, **kw)
        return locked

    def __setattr__(self, name, value):
        setattr(self._inner, name, value)


class Target:
    """Common surface: .backend, .sdcard, start(), stop()."""

    # gwemu replays a .tl itself (GNW_TIMELINE); hardware needs a player.
    timeline_is_builtin = False

    def __init__(self, image):
        self.image = image
        self.backend = None
        self.sdcard = None
        self.player = None

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def collect_artifacts(self, dest="build/artifacts"):
        return []


class GwemuTarget(Target):
    """Boot an Image under gwemu."""

    timeline_is_builtin = True

    def __init__(self, image, display=False, audio=False, icount=None,
                 timeline=None, record=None, port=None, qmp_port=None,
                 keep_temp=False, stdio_gdb=False):
        super().__init__(image)
        self.display = display
        self.audio = audio
        self.icount = icount
        self.timeline = timeline
        self.record = record
        self.stdio_gdb = stdio_gdb
        self.port = port or (None if stdio_gdb else _free_port())
        self.qmp_port = qmp_port or (None if stdio_gdb else _free_port())
        self.proc = None
        self.qmp = None
        self.keep_temp = keep_temp
        # Isolate: gwemu hardcodes relative image names, and concurrent runs out
        # of one directory corrupt each other (docs/04 §3).
        self.tmpdir = _make_run_dir()
        # Belt and braces: if a harness dies before stop(), still clean up.
        atexit.register(self._cleanup)

    def _pad_bank(self, src, name):
        dst = os.path.join(self.tmpdir, name)
        data = b""
        if src and os.path.exists(src):
            with open(src, "rb") as fh:
                data = fh.read()
        if len(data) > BANK_SIZE:
            raise RuntimeError(f"{src} is {len(data)}B, larger than a bank")
        with open(dst, "wb") as fh:
            fh.write(data.ljust(BANK_SIZE, b"\x00"))
        return dst

    def start(self):
        try:
            self._start()
        except Exception:
            self.stop()          # never leave a populated run dir behind
            raise

    def _start(self):
        img = self.image
        self._pad_bank(img.bank1, "qemu_bank1.bin")
        self._pad_bank(img.bank2, "qemu_bank2.bin")
        for src, name in ((img.extflash, "extflash.bin"),
                          (img.sdcard, "sdcard.img")):
            if src and os.path.exists(src):
                shutil.copy(src, os.path.join(self.tmpdir, name))

        cmd = ["gwemu", "-machine", "gnw-h7b0",
               "-global", "gnw-h7b0-soc.bank1-image=qemu_bank1.bin",
               "-global", "gnw-h7b0-soc.bank2-image=qemu_bank2.bin"]
        if os.path.exists(os.path.join(self.tmpdir, "extflash.bin")):
            cmd += ["-global", "gnw-h7b0-soc.extflash-image=extflash.bin"]
        # Silent by default: these run in the background repeatedly.
        # sdl3, not sdl: gwemu is built against SDL3 and the older backend
        # name makes it fail SDL audio init and exit before QMP ever opens.
        cmd += ["-audiodev", ("sdl3,id=snd0" if self.audio else "none,id=snd0"),
                "-global", "gnw-h7b0-sai1.audiodev=snd0"]
        if os.path.exists(os.path.join(self.tmpdir, "sdcard.img")):
            cmd += ["-drive", "if=sd,format=raw,file=sdcard.img"]
        cmd += ["-display", "gwemu" if self.display else "none"]
        if self.stdio_gdb:
            cmd += ["-gdb", "stdio", "-S"]
        else:
            cmd += ["-qmp", f"tcp:localhost:{self.qmp_port},server=on,wait=off",
                    "-gdb", f"tcp::{self.port}", "-S"]
        if self.icount is not None:
            # Without -icount, virtual time follows host wall-clock and DWT
            # counts vary ~14% run to run. See docs/06.
            cmd += ["-icount", f"shift={self.icount},align=off,sleep=off"]

        # Pipe mode has no QMP, so use the timeline's own quit event as the
        # process lifetime bound instead of leaving a background emulator.
        if self.stdio_gdb and not self.timeline:
            env_timeline = os.path.join(os.path.dirname(__file__), "..", "..",
                                        "timelines", "probe-quit.tl")
            self.timeline = os.path.abspath(env_timeline)

        env = dict(os.environ)
        from ..timeline_launch import configure_timeline
        configure_timeline(env, timeline=self.timeline, record_timeline=self.record,
                           headless=not self.display)
        self.proc = subprocess.Popen(
            cmd, cwd=self.tmpdir, env=env,
            stdin=subprocess.PIPE if self.stdio_gdb else None,
            stdout=subprocess.PIPE if self.stdio_gdb else None)
        time.sleep(1.0)
        if self.stdio_gdb:
            self.qmp = None
            self.backend = GDBPipeBackend(self.proc)
        else:
            self.qmp = QMP(self.qmp_port)
            self.backend = GDBBackend(host="localhost", port=self.port)
        self.backend.open()
        self.sdcard = QemuSDCardManager(os.path.join(self.tmpdir, "sdcard.img"))
        self.backend.resume()

    def press(self, *names, delay=0.5):
        for n in names:
            self.qmp.send_key(n)
            time.sleep(delay)

    def collect_artifacts(self, dest="build/artifacts"):
        os.makedirs(dest, exist_ok=True)
        out = []
        for name in sorted(os.listdir(self.tmpdir)):
            if name.endswith((".png", ".ppm")):
                shutil.copy(os.path.join(self.tmpdir, name),
                            os.path.join(dest, name))
                out.append(os.path.join(dest, name))
        if out:
            print(f"artifacts -> {dest}/ ({len(out)} files)")
        return out

    def _cleanup(self):
        if self.keep_temp or not self.tmpdir:
            return
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        self.tmpdir = None

    def stop(self):
        try:
            if self.qmp:
                self.qmp.close()
            if self.backend:
                self.backend.close()
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        finally:
            if self.keep_temp:
                print(f"kept run dir: {self.tmpdir}")
            self._cleanup()

    # Context-manager form so cleanup is structural, not remembered.
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
        return False


class HardwareTarget(Target):
    """Boot an Image on the real device over a debug probe."""

    def __init__(self, image, physical_sdcard=None, no_flash=False,
                 hard_reset=False, erase_first=False, precise_faults=False,
                 timeline=None):
        super().__init__(image)
        self.physical_sdcard = physical_sdcard
        self.no_flash = no_flash
        self.hard_reset = hard_reset
        self.erase_first = erase_first
        self.precise_faults = precise_faults
        # The SAME --timeline file gwemu replays internally. Here it is played
        # back by writing the REMOTE_INPUT shadow word over the probe, so a
        # test does not have to know which tier it is on. Needs a firmware
        # built with REMOTE_INPUT=1 (scripts/common/retrogo_build.py).
        self.timeline = timeline
        self.gnw = None

    def start(self):
        print("Attaching to hardware via OpenOCD...")
        self.backend = _SerialBackend(
            AutoOpenOCDBackend(operation="gwprov hardware target"), threading.Lock())
        self.backend.open()

        # OBSERVE-ONLY: attach without disturbing what is running.
        #
        # gnw.start_gnwmanager() is NOT a passive handshake -- it does
        # reset_and_halt(), loads gnwmanager's own firmware into RAM and jumps
        # to it, i.e. it destroys the running application. It is needed only to
        # flash and to reach the SD card; reading memory, registers, the LTDC
        # registers and the framebuffer needs nothing but an attached probe.
        # Starting it unconditionally is why reading a benchmark's result used
        # to restart the benchmark.
        if getattr(self, "no_flash", False):
            # --no-flash --hard-reset = reboot what is already on the device,
            # touching no flash at all. Needed because REPROGRAMMING is itself
            # destructive on this part (see erase_first): once a good image is
            # on there, the way to boot it is a reset, not another flash.
            if self.hard_reset:
                print("--no-flash --hard-reset: resetting the device, "
                      "leaving flash untouched")
                self.backend.reset_and_halt()
                if self.precise_faults:
                    self._enable_precise_faults()
                self.backend.resume()
                self._start_timeline()
                return
            print("--no-flash: observing the device WITHOUT reset "
                  "(no gnwmanager, no flash, no SD)")
            self._start_timeline()
            return

        from ..hw_lifecycle import (assert_application_mode,
                                    mark_backend_recovery_required)
        assert_application_mode(self.backend, operation="start a hardware target")
        mark_backend_recovery_required(self.backend, "starting gnwmanager RAM programmer")
        self.gnw = GnW(self.backend)
        self.gnw.start_gnwmanager()
        mark_backend_recovery_required(self.backend, "gnwmanager RAM programmer")

        # Flash the bank the image was LINKED for. A bank-2 firmware written to
        # bank 1 boots nothing, and this used to be hardcoded to bank 1.
        bank = self.image.intflash_bank
        src = self.image.hw_primary
        # ERASE BEFORE PROGRAM. On STM32H7 the flash is ECC-protected in
        # 128-bit words: programming a word that was not erased first leaves an
        # UNCORRECTABLE ECC error, and reading that word raises a BUS FAULT --
        # imprecise, so it reports no address, and it survives reflashing
        # because the bad word is simply reprogrammed over again. A debugger
        # read-back can still compare equal while an instruction fetch faults,
        # which makes it look like the image is fine.
        if self.erase_first:
            with open(src, "rb") as fh:
                nbytes = len(fh.read())
            print(f"Erasing bank {bank} ({nbytes} bytes) before programming...")
            self.gnw.erase(bank, 0, nbytes)

        print(f"Flashing {src} to internal flash bank {bank}...")
        with open(src, "rb") as fh:
            self.gnw.flash(bank, 0, fh.read())

        self.sdcard = (HardwarePhysicalSDCardManager(self.physical_sdcard)
                       if self.physical_sdcard
                       else HardwareProbeSDCardManager(self.gnw))

        self._start_firmware()
        self._start_timeline()


    # SCB->CCR bit 1 (DISDEFWBUF) turns off the write buffer, which makes bus
    # faults PRECISE: BFAR and the stacked PC then point at the instruction
    # that actually faulted. Without it an imprecise fault reports no address
    # at all -- which is why a boot fault here could not be localised. Costs
    # performance, so it is a diagnostic switch, not a default. Must be set
    # while halted BEFORE the faulting code runs.
    CCR_ADDR = 0xE000ED14
    DISDEFWBUF = 1 << 1

    def _enable_precise_faults(self):
        ccr = self.backend.read_uint32(self.CCR_ADDR)
        self.backend.write_uint32(self.CCR_ADDR, ccr | self.DISDEFWBUF)
        got = self.backend.read_uint32(self.CCR_ADDR)
        print(f"precise faults: SCB->CCR 0x{ccr:08x} -> 0x{got:08x} "
              f"(DISDEFWBUF {'set' if got & self.DISDEFWBUF else 'FAILED'})")

    def _start_firmware(self):
        """Boot the flashed firmware, the way `gnwmanager start` does it.

        A plain reset_and_halt()+resume() is NOT enough once gnwmanager's stub
        has been loaded, and that is what this used to do: it resumed into
        whatever state the stub left rather than into the firmware, so the
        device sat there not booting after a flash that had actually written
        the right bytes. gnwmanager's own `start` subcommand is explicit about
        it ("Do NOT start gnwmanager") -- it reloads MSP and PC from the
        target bank's vector table before resuming.
        """
        # A TRUE hardware reset, when asked for. The MSP/PC route below is a
        # jump to the reset vector, NOT a reset: every peripheral (OCTOSPI,
        # LTDC, SDMMC) keeps the configuration the previous session left, and
        # SystemInit then reprograms the clocks underneath them. A write still
        # buffered in a peripheral that way surfaces as an IMPRECISE bus fault
        # at the first `dsb` in SystemInit's D-cache invalidate loop -- early
        # enough that the backlight never comes on, and impossible to localise
        # because imprecise faults do not report an address. Use this when the
        # device must start from the same state a power cycle gives it.
        if self.hard_reset:
            print("hard reset: resetting the CPU and every peripheral")
            self.gnw.reset_and_halt()
            if self.precise_faults:
                self._enable_precise_faults()
            self.backend.resume()
            self._confirm_application_running()
            return

        base = 0x08100000 if self.image.intflash_bank == 2 else 0x08000000
        self.gnw.reset_and_halt()
        self.backend.write_register("msp", self.gnw.read_uint32(base))
        self.backend.write_register("pc", self.gnw.read_uint32(base + 4))
        self.backend.resume()
        self._confirm_application_running()

    def _confirm_application_running(self):
        from ..hw_lifecycle import (clear_backend_recovery_required,
                                    inspect_target_mode)
        state = inspect_target_mode(self.backend)
        if state["halted"] or state["stubResident"]:
            raise RuntimeError(
                "firmware did not leave gnwmanager Recovery Mode; GWProv is "
                "holding the target traffic guard. Check `gwprov ps` and use "
                "`gwprov device recover` when the mailbox is idle."
            )
        clear_backend_recovery_required(self.backend)

    def _start_timeline(self):
        """Begin replaying --timeline over the probe, from THIS instant.

        t0 is the moment the firmware was released to run, which is the same
        origin gwemu uses for the file it replays internally -- so a timeline
        recorded under gwemu lines up on silicon without being rewritten.
        Playback runs on its own thread; the harness keeps its main thread.
        """
        if not self.timeline:
            return
        events = parse(self.timeline)
        self.player = ShadowWordPlayer(self.backend, events)
        print(f"[timeline] replaying {self.timeline} on hardware "
              f"(needs a REMOTE_INPUT=1 firmware)")
        self.player.start()

    def stop(self):
        if self.player:
            self.player.stop()
            print(self.player.report())
        proc = getattr(self.backend, "_openocd_process", None)
        if proc:
            proc.terminate()
            proc.wait()


def add_target_args(parser):
    """Shared CLI surface. Every harness gets the same switches."""
    # "qemu" is accepted as an alias for "gwemu": existing harnesses and docs
    # use it, and the emulator is a QEMU fork.
    parser.add_argument("--target", choices=["gwemu", "qemu", "hardware"],
                        default="gwemu", help="where to run")
    parser.add_argument("--gui", action="store_true",
                        help="show the gwemu window")
    parser.add_argument("--audio", action="store_true",
                        help="enable host audio (silent by default)")
    parser.add_argument("--icount", default=None,
                        help="gwemu -icount shift; makes DWT counts "
                             "reproducible (see docs/06)")
    parser.add_argument("--timeline", default=None,
                        help="input timeline (.tl) to replay. gwemu replays it "
                             "itself; on hardware it is injected through the "
                             "REMOTE_INPUT shadow word, so the same file drives "
                             "both tiers")
    parser.add_argument("--record", default=None, metavar="FILE.tl",
                        help="RECORD an input timeline from an interactive "
                             "session. Implies a window; quit gwemu cleanly or "
                             "nothing is written")
    parser.add_argument("--physical-sdcard", default=None,
                        help="mounted SD path for bulk hardware copies")
    parser.add_argument("--precise-faults", action="store_true",
                        help="disable the write buffer (SCB->CCR DISDEFWBUF) "
                             "at reset so bus faults are PRECISE and BFAR/PC "
                             "identify the faulting instruction. Slower; use "
                             "it to localise an imprecise fault")
    parser.add_argument("--erase-first", action="store_true",
                        help="erase internal flash before programming it. "
                             "Programming H7 flash that was not erased leaves "
                             "an uncorrectable ECC error whose READ raises a "
                             "bus fault -- and which survives reflashing")
    parser.add_argument("--hard-reset", action="store_true",
                        help="start the device with a REAL reset (CPU and "
                             "peripherals) instead of jumping to the reset "
                             "vector. Use when peripheral state left by a "
                             "previous session may be poisoning the boot")
    parser.add_argument("--no-flash", action="store_true",
                        help="attach to hardware WITHOUT flashing or resetting "
                             "-- observe what is already running")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the run directory under build/run/ for "
                             "analysis (deleted by default -- each run copies "
                             "the SD image and extflash)")
    parser.add_argument("--stdio-gdb", action="store_true",
                        help="use gwemu GDB over subprocess pipes when local "
                             "sockets are unavailable")


def open_target(args, image):
    """Build the Target an argparse namespace asks for."""
    if getattr(args, "target", "gwemu") == "hardware":
        return HardwareTarget(image, physical_sdcard=args.physical_sdcard,
                              no_flash=getattr(args, "no_flash", False),
                              hard_reset=getattr(args, "hard_reset", False),
                              erase_first=getattr(args, "erase_first", False),
                              precise_faults=getattr(args, "precise_faults",
                                                     False),
                              timeline=getattr(args, "timeline", None))
    return GwemuTarget(image, display=getattr(args, "gui", False),
                       audio=getattr(args, "audio", False),
                       icount=getattr(args, "icount", None),
                       timeline=getattr(args, "timeline", None),
                       record=getattr(args, "record", None),
                       keep_temp=getattr(args, "keep_temp", False),
                       stdio_gdb=getattr(args, "stdio_gdb", False))
