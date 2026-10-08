"""Launch a visible GWemu instance under GDB with its matching firmware ELF."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path


def debug_profile(profile_dir: str, *, gdb_port: int = 1234,
                  qmp_socket: str | None = None,
                  symbols: str | None = None, gdb: str | None = None,
                  audio: bool = False, break_on_fault: bool = True,
                  unpause_homebrew: bool = False,
                  app_symbols: str | None = None,
                  detach_after_app_entry: bool = False,
                  keep_running: bool = False) -> int:
    """Run a provisioned profile in GWemu and hand control to interactive GDB."""
    from .profiles import DeviceProfile

    profile = DeviceProfile.load(profile_dir)
    for path in (profile.bank1, profile.bank2, profile.extflash):
        if not path.is_file():
            raise ValueError(f"profile image is missing: {path}")
    symbol_file = Path(symbols).expanduser().resolve() if symbols else \
        profile.root / "debug" / "retro-go-debug.elf"
    if not symbol_file.is_file():
        raise ValueError(
            f"matching firmware symbols not found: {symbol_file}; recreate the profile "
            "from firmware staged by `gwprov retro-go install`")
    gdb_bin = gdb or shutil.which("arm-none-eabi-gdb") or shutil.which("gdb-multiarch")
    if not gdb_bin:
        raise ValueError("install arm-none-eabi-gdb or gdb-multiarch to use gwprov gwemu debug")

    cmd = ["gwemu", "-machine", "gnw-h7b0"]
    for prop, path in (("bank1-image", profile.bank1), ("bank2-image", profile.bank2),
                       ("extflash-image", profile.extflash)):
        cmd += ["-global", f"gnw-h7b0-soc.{prop}={path}"]
    cmd += ["-display", "gwemu", "-audiodev", "sdl3,id=snd0" if audio else "none,id=snd0",
            "-global", "gnw-h7b0-sai1.audiodev=snd0", "-S",
            "-gdb", f"tcp:127.0.0.1:{gdb_port}"]
    if qmp_socket:
        qmp_path = Path(qmp_socket).expanduser().resolve()
        qmp_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-qmp", f"unix:{qmp_path},server=on,wait=off"]
    if profile.resolved_sd:
        cmd += ["-drive", f"if=sd,format=raw,file={profile.resolved_sd}"]
    config = profile.root / "gwemu.toml"
    if not config.exists():
        config.write_text("[general]\nshow_welcome = false\n")
    cmd += ["-config_path", str(config)]
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(profile.root / "runtime")
    env["XDG_CONFIG_HOME"] = str(profile.root / "runtime/config")

    process = subprocess.Popen(cmd, cwd=profile.root, env=env,
                                   start_new_session=True)
    try:
        # Let GWemu start before GDB connects. Avoid a probe connection here:
        # QEMU's GDB stub treats every connection as the debugger and can leave
        # the target stopped when a readiness-check socket disconnects.
        time.sleep(1.0)
        if process.poll() is not None:
            raise RuntimeError(f"GWemu exited with status {process.returncode}")

        gdb_script = profile.root / "runtime" / "gwprov" / "debug.gdb"
        gdb_script.parent.mkdir(parents=True, exist_ok=True)
        lines = ["set pagination off",
                 f"target extended-remote 127.0.0.1:{gdb_port}"]
        breakpoint_number = 1
        if break_on_fault:
            fault_report = profile.root / "runtime" / "gwprov" / "fault-triage.txt"
            fault_symbols = ("common_fault_handler_c", "HardFault_Handler",
                             "BusFault_Handler", "UsageFault_Handler",
                             "Error_Handler", "abort")
            for symbol in fault_symbols:
                lines += [f"break {symbol}", f"commands {breakpoint_number}",
                          "silent",
                          f"set logging file \"{fault_report}\"",
                          "set logging overwrite on", "set logging enabled on",
                          f'printf "GWPROV_FAULT {symbol}\\n"',
                          "bt", "info registers", "x/16wx $sp", "x/12i $pc-12",
                          "set logging enabled off",
                          f'printf "gwprov: fault triage saved to {fault_report}\\n"',
                          "end"]
                breakpoint_number += 1
        if detach_after_app_entry and (not unpause_homebrew or not app_symbols):
            raise ValueError("--detach-after-app-entry requires --unpause-homebrew and --app-symbols")
        if unpause_homebrew:
            # CONFIG autostart enters run_gwhb_homebrew(path, load_state,
            # start_paused, save_slot). At function entry AAPCS places the
            # third argument in r2; clearing it keeps the app's normal pause
            # behavior intact while allowing unattended debug iteration.
            lines += ["break run_gwhb_homebrew", f"commands {breakpoint_number}",
                      "silent", "set $r2 = 0",
                      'printf "gwprov: cleared Retro-Go start_paused for this launch\\n"',
                      "continue", "end"]
        if detach_after_app_entry:
            from .debug_shell import SymbolTable
            app_elf = Path(app_symbols).expanduser().resolve()
            app_table = SymbolTable()
            app_table.load(app_elf)
            app_entry = app_table["app_main"] & ~1
            lines += [f"hbreak *0x{app_entry:x}",
                      f"commands {breakpoint_number + 1}", "silent",
                      "disable breakpoints", "detach", "quit", "end"]
            breakpoint_number += 1
        lines += ["continue"]
        gdb_script.write_text("\n".join(lines) + "\n")
        gdb_cmd = [str(gdb_bin), "-q", str(symbol_file), "-x", str(gdb_script)]
        print(f"Firmware symbols: {symbol_file}", flush=True)
        if unpause_homebrew:
            print("Debug launch hook: intercept run_gwhb_homebrew and clear r2 "
                  "(start_paused) before entering the app.", flush=True)
        if detach_after_app_entry:
            print(f"Debug launch hook: detach at app_main 0x{app_entry:08x}; "
                  "the guest continues running.", flush=True)
        print("GDB controls the visible GWemu session. Use continue, stepi, "
              "info registers, x/16wx ADDRESS, monitor system_reset, and bt.",
              flush=True)
        if qmp_socket:
            print(f"QMP controls: {qmp_path}", flush=True)
        return subprocess.run(gdb_cmd, check=False).returncode
    except KeyboardInterrupt:
        return 130
    finally:
        if process.poll() is None and not keep_running:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
