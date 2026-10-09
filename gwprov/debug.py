"""Launch a GWemu instance under GDB with its matching firmware ELF."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path


def debug_profile(profile_dir: str, *, gdb_port: int = 1234, headless: bool = False,
                  qmp_socket: str | None = None, qmp_enabled: bool = True,
                  symbols: str | None = None, gdb: str | None = None,
                  audio: bool = False, break_on_fault: bool = True,
                  unpause_homebrew: bool = False,
                  app_symbols: str | None = None,
                  detach_after_app_entry: bool = False,
                  keep_running: bool = False, timeline: str | None = None,
                  record_timeline: str | None = None) -> int:
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
    cmd += ["-display", "none" if headless else "gwemu",
            "-audiodev", "sdl3,id=snd0" if audio else "none,id=snd0",
            "-global", "gnw-h7b0-sai1.audiodev=snd0", "-S",
            "-gdb", f"tcp:127.0.0.1:{gdb_port}"]
    qmp_path = None
    if qmp_enabled:
        if qmp_socket is None:
            owner = str(os.getuid()) if hasattr(os, "getuid") else str(os.getpid())
            profile_id = hashlib.sha256(os.fsencode(profile.root)).hexdigest()[:20]
            qmp_path = (Path.home() / ".cache" / "gwprov" / "qmp"
                        / f"{owner}-{profile_id}.sock")
        else:
            qmp_path = Path(qmp_socket).expanduser().resolve()
        if len(os.fsencode(qmp_path)) >= 104:
            raise ValueError(
                f"QMP socket path exceeds the portable Unix socket limit: {qmp_path}; "
                "pass a shorter --qmp-socket path or use --no-qmp")
        if qmp_path.exists():
            raise RuntimeError(
                f"QMP socket already exists: {qmp_path}; check for an existing GWemu "
                "instance before removing it")
        qmp_path.parent.mkdir(parents=True, exist_ok=True)
        cmd += ["-qmp", f"unix:{qmp_path},server=on,wait=off"]
    elif qmp_socket is not None:
        raise ValueError("--qmp-socket cannot be used with QMP disabled")
    if profile.resolved_sd:
        cmd += ["-drive", f"if=sd,format=raw,file={profile.resolved_sd}"]
    config = profile.root / "gwemu.toml"
    if not config.exists():
        config.write_text("[general]\nshow_welcome = false\n")
    if not headless:
        cmd += ["-config_path", str(config)]
    env = dict(os.environ)
    env["XDG_DATA_HOME"] = str(profile.root / "runtime")
    env["XDG_CONFIG_HOME"] = str(profile.root / "runtime/config")

    from .timeline_launch import configure_timeline
    configure_timeline(env, timeline=timeline, record_timeline=record_timeline)

    # Validate every option needed to build the GDB script before launching
    # GWemu. It starts halted with -S; raising after Popen leaves an orphaned
    # VM that appears to the user as a mysterious startup pause.
    app_elf = Path(app_symbols).expanduser().resolve() if app_symbols else None
    app_entry = None
    if app_elf and not app_elf.is_file():
        raise ValueError(f"app symbols not found: {app_elf}")
    if detach_after_app_entry and (not unpause_homebrew or not app_elf):
        raise ValueError("--detach-after-app-entry requires --unpause-homebrew and --app-symbols")
    if detach_after_app_entry:
        from .debug_shell import SymbolTable
        app_table = SymbolTable()
        app_table.load(app_elf)
        app_entry = app_table["app_main"] & ~1

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
        if app_elf:
            # Load app symbols even in an attached session so fault PCs in the
            # homebrew resolve alongside the official firmware symbols.
            lines.append(f'add-symbol-file "{app_elf}"')
        breakpoint_number = 1
        if break_on_fault:
            fault_report = profile.root / "runtime" / "gwprov" / "fault-triage.txt"
            # GDB treats the remainder of `set logging file` as the literal
            # path, including quote marks. Keep the path unquoted and enable
            # logging before execution so fault commands are captured.
            lines += [f"set logging file {fault_report}",
                      "set logging overwrite on", "set logging enabled on"]
            fault_symbols = ("common_fault_handler_c", "HardFault_Handler",
                             "BusFault_Handler", "UsageFault_Handler",
                             "Error_Handler", "abort")
            for symbol in fault_symbols:
                lines += [f"break *{symbol}", f"commands {breakpoint_number}",
                          "silent",
                          f'printf "GWPROV_FAULT {symbol}\\n"',
                          "bt", "info registers",
                          "set $gwprov_exc_return = $lr",
                          "if (($gwprov_exc_return & 0xffffff00) == 0xffffff00)",
                          "set $gwprov_exc_sp = (($gwprov_exc_return & 4) != 0) ? $psp : $msp",
                          "set $gwprov_core_sp = $gwprov_exc_sp",
                          'printf "GWPROV_EXCEPTION_FRAME exc_return=0x%08x exception_sp=0x%08x core_frame=0x%08x extended_fp=%d\\n", $gwprov_exc_return, $gwprov_exc_sp, $gwprov_core_sp, (($gwprov_exc_return & 16) == 0)',
                          "x/8wx $gwprov_core_sp",
                          "set $gwprov_fault_pc = *(unsigned int *)($gwprov_core_sp + 24)",
                          "set $gwprov_fault_lr = *(unsigned int *)($gwprov_core_sp + 20)",
                          "set $gwprov_fault_xpsr = *(unsigned int *)($gwprov_core_sp + 28)",
                          'printf "GWPROV_STACKED pc=0x%08x lr=0x%08x xpsr=0x%08x\\n", $gwprov_fault_pc, $gwprov_fault_lr, $gwprov_fault_xpsr',
                          'printf "GWPROV_SCB CFSR=0x%08x HFSR=0x%08x MMFAR=0x%08x BFAR=0x%08x ABFSR=0x%08x\\n", *(unsigned int *)0xe000ed28, *(unsigned int *)0xe000ed2c, *(unsigned int *)0xe000ed34, *(unsigned int *)0xe000ed38, *(unsigned int *)0xe000efa8',
                          "if $gwprov_fault_pc != 0",
                          "info symbol $gwprov_fault_pc",
                          "x/12i ($gwprov_fault_pc & ~1)-12",
                          "end",
                          "else",
                          'printf "GWPROV_EXCEPTION_FRAME unavailable: LR=0x%08x is not EXC_RETURN\\n", $gwprov_exc_return',
                          "end",
                          "x/16wx $sp", "x/12i $pc-12",
                          f'printf "gwprov: fault triage saved to {fault_report}\\n"',
                          "end"]
                breakpoint_number += 1
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
        print("GDB controls the GWemu session. Use continue, stepi, "
              "info registers, x/16wx ADDRESS, monitor system_reset, and bt.",
              flush=True)
        if qmp_path:
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
