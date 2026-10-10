"""Command-line entry point for common provisioning operations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path

from rich.console import Console
from rich.console import Group
from rich.text import Text
from rich.tree import Tree
from rich_argparse import RawDescriptionRichHelpFormatter


RawDescriptionRichHelpFormatter.help_markup = False
RawDescriptionRichHelpFormatter.text_markup = False


class GWProvArgumentParser(argparse.ArgumentParser):
    """Use the same terminal-aware help style at every command level."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", RawDescriptionRichHelpFormatter)
        super().__init__(*args, **kwargs)

    def format_help(self) -> str:
        categories = getattr(self, "help_categories", None)
        if not categories:
            hidden_choices = []
            for action in self._actions:
                if isinstance(action, argparse._SubParsersAction):
                    original = action._choices_actions
                    action._choices_actions = [choice for choice in original
                                               if choice.help != argparse.SUPPRESS]
                    hidden_choices.append((action, original))
            try:
                return super().format_help()
            finally:
                for action, original in hidden_choices:
                    action._choices_actions = original

        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions,
                            self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        action_groups = sorted(
            self._action_groups,
            key=lambda group: 0 if group.title.lower() == "options" else 1)
        for action_group in action_groups:
            subparsers = [action for action in action_group._group_actions
                          if isinstance(action, argparse._SubParsersAction)]
            ordinary = [action for action in action_group._group_actions
                        if not isinstance(action, argparse._SubParsersAction)]
            if not subparsers:
                formatter.start_section(action_group.title)
                formatter.add_text(action_group.description)
                formatter.add_arguments(ordinary)
                formatter.end_section()
                continue
            if ordinary or action_group.description:
                formatter.start_section(action_group.title)
                formatter.add_text(action_group.description)
                formatter.add_arguments(ordinary)
                formatter.end_section()
            if subparsers:
                action = subparsers[0]
                by_name = {choice.dest: choice for choice in action._choices_actions}
                shown: set[str] = set()
                for title, names in categories:
                    choices = [by_name[name] for name in names if name in by_name]
                    shown.update(name for name in names if name in by_name)
                    if choices:
                        formatter.start_section(title)
                        formatter.add_arguments(choices)
                        formatter.end_section()
                remaining = [choice for choice in action._choices_actions
                             if choice.dest not in shown]
                if remaining:
                    formatter.start_section("Other commands")
                    formatter.add_arguments(remaining)
                    formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _build_retro_go(args) -> int:
    from gwprov.common.retrogo_build import build

    result = build(
        path=args.path,
        clean=args.clean,
        target=args.make_target,
        intflash_bank=args.bank,
        extflash_part_mb=args.extflash_part_mb,
        extflash_offset_mb=args.extflash_offset_mb,
        jobs=args.jobs,
        docker=args.docker,
        extra=dict(item.split("=", 1) for item in args.makevar),
        verbose=not args.quiet,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        return 0
    print(json.dumps(asdict(result), indent=2, default=str))
    return 0


def _firmware_install(args) -> int:
    from gwprov.dist.firmware import install_firmware
    result = install_firmware(args.output, variant=args.variant, repo=args.repo, version=args.version)
    print(f"Installed Retro-Go {result['version']} ({args.variant}) in {args.output}")
    return 0


def _project_stage_local(args) -> int:
    from gwprov.dist.local_project import stage_local_project
    result = stage_local_project(args.manifest, output=args.output)
    print(f"Staged local {result['repo']}:{result['target']} ({result['variant']})")
    for path in result['files']:
        print(f"  {path}")
    return 0


def _profile_create(args) -> int:
    from gwprov.profiles import profile_destination

    destination = profile_destination(args.directory, output_dir=args.output_dir)
    if args.stock:
        if not args.backup_dir:
            raise ValueError("--stock requires at least one --backup-dir")
        from gwprov.stock import create_stock_profile
        result = create_stock_profile(destination, backup_dirs=args.backup_dir,
                                      locked=args.locked, model=args.model or "auto",
                                      extflash_mib=args.extflash_mib or 64)
        print(f"Created stock {result['model']} profile: {destination}; locked={result['locked']}")
        return 0
    if not args.content:
        raise ValueError("profile create requires --content, or use --stock with --backup-dir")
    if args.backup_dir or args.locked or args.model is not None:
        raise ValueError("--backup-dir, --locked, and --model require --stock")
    from gwprov.provision import create_profile
    report = create_profile(destination, content=args.content, name=args.name,
                            littlefs_mib=args.littlefs_mib, extflash_mib=args.extflash_mib,
                            sd_size_mib=args.sd_size_mib, sd_label=args.sd_label,
                            bootloader_repo=args.bootloader_repo,
                            bootloader_version=args.bootloader_version,
                            bootloader_file=args.bootloader_file)
    if report.get('variant') == 'sd':
        print(f"Created {destination}: SD image {report['layout']['sdImageBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    else:
        print(f"Created {destination}: FrogFS {report['layout']['frogfsBytes']} bytes; "
              f"LittleFS {report['layout']['littlefsBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    print(f"Boot with: gwprov gwemu run --profile {destination}")
    return 0


def _profile_duplicate(args) -> int:
    from gwprov.profiles import duplicate_profile

    result = duplicate_profile(args.source, args.destination, output_dir=args.output_dir)
    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        console.print("[bold green]Profile duplicated[/]")
        console.print(f"  [dim]From[/]  {result['source']}")
        console.print(f"  [dim]To[/]    [bold cyan]{result['destination']}[/]")
        console.print(f"  [dim]Files[/] {result['files']}")
        console.print(f"\nRun it with [bold]gwprov gwemu run --profile {result['destination']}[/]")
    return 0


def _profile_list(args) -> int:
    from gwprov.profiles import list_profiles, profile_directory

    root = profile_directory()
    rows = list_profiles()
    if args.output == "names":
        for row in rows:
            print(row["name"])
        return 0
    if args.output == "json":
        print(json.dumps({"profileDirectory": str(root), "profiles": rows}, indent=2))
        return 0
    console = Console()
    if not rows:
        console.print(f"[dim]No profiles found in {root}.[/]")
        console.print("Create one with [bold]gwprov profile create NAME --content DIR[/].")
        return 0
    from rich.table import Table
    table = Table(title=f"[bold bright_cyan]Device profiles[/] [dim]· {len(rows)}",
                  title_justify="left", expand=True, show_lines=False)
    table.add_column("NAME", style="bold cyan", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("DEVICE / FIRMWARE", overflow="fold")
    table.add_column("LOCATION", overflow="fold")
    for row in rows:
        details = " · ".join(value for value in (row["model"], row["firmware"]) if value) or "—"
        state = {
            "ready": "[bold green]Ready[/]",
            "incomplete": "[bold yellow]Incomplete[/]",
            "invalid": "[bold red]Invalid[/]",
        }[row["status"]]
        if row["error"]:
            details = row["error"]
        table.add_row(row["name"], state, details, row["path"])
    line_count = len(console.render_lines(table, console.options))
    if console.is_terminal and not args.no_pager and line_count > console.height:
        with console.pager(styles=True):
            console.print(table)
    else:
        console.print(table)
    return 0


def _make_config(args) -> int:
    from gwprov.common.retrogo_config import build, rom_path

    startup_file = rom_path(*args.rom) if args.rom else ""
    blob = build(
        startup_file=startup_file,
        menu_timeout_s=args.menu_timeout,
        cpu_oc_level=args.oc_level,
        selected_tab=args.selected_tab,
        cursor=args.cursor,
        browse_subpath=args.browse_subpath,
    )
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(blob)
    print(f"wrote {len(blob)} bytes to {output}")
    return 0


def _run_gwemu(args) -> int:
    if args.profile:
        if args.stdio_gdb:
            raise ValueError("profile launch runs freely; use --gdb-port for an optional debug connection")
        from gwprov.daemon import start_instance
        row = start_instance(args.profile, headless=args.headless, audio=args.audio,
                             timeline=args.timeline, record_timeline=args.record_timeline,
                             gdb_port=args.gdb_port, shared_sd_root=args.shared_sd_root,
                             timing_mode=args.timing_mode, icount=args.icount, rtc_epoch=args.rtc_epoch, gwemu_bin=args.gwemu_bin)
    else:
        if args.stdio_gdb:
            if args.timing_mode != "default" or args.rtc_epoch is not None or args.gwemu_bin is not None:
                raise ValueError("timing-mode, rtc-epoch and gwemu-bin require daemon-managed GWemu; omit --stdio-gdb")
            # GDB stdio consumes QEMU's stdin/stdout, so this legacy harness
            # transport cannot share the daemon's QMP stdio channel.
            from gwprov.common.target import GwemuTarget, Image
            target = GwemuTarget(
                Image(bank1=args.bank1, bank2=args.bank2, extflash=args.extflash,
                      sdcard=args.sdcard, intflash_bank=args.bank),
                display=not args.headless, audio=args.audio, icount=args.icount,
                timeline=args.timeline, record=args.record_timeline,
                keep_temp=args.keep_temp, stdio_gdb=True)
            try:
                target.start()
                print("GWemu running in the direct GDB-stdio harness; press Ctrl-C to stop.",
                      flush=True)
                while target.proc is not None and target.proc.poll() is None:
                    time.sleep(0.5)
                return target.proc.returncode if target.proc else 0
            except KeyboardInterrupt:
                return 130
            finally:
                target.stop()
        from gwprov.daemon import start_image
        row = start_image({"bank1": args.bank1, "bank2": args.bank2,
                           "extflash": args.extflash, "sdcard": args.sdcard},
                          headless=args.headless, audio=args.audio,
                          timeline=args.timeline, record_timeline=args.record_timeline,
                          icount=args.icount, keep_temp=args.keep_temp,
                          timing_mode=args.timing_mode, rtc_epoch=args.rtc_epoch, gwemu_bin=args.gwemu_bin)
    from gwprov.active_device import set_active
    set_active(f"gwemu:{row['pid']}")
    from gwprov.gwemu_manager import stop_instance
    import psutil
    process = psutil.Process(row["pid"])
    print(f"GWemu running under the GWProv daemon (pid {row['pid']}); press Ctrl-C to stop.",
          flush=True)
    from gwprov.active_device import get_active, set_active
    try:
        result = process.wait()
        if get_active() == f"gwemu:{row['pid']}":
            set_active(None)
        return result
    except KeyboardInterrupt:
        stop_instance(pid=row["pid"])
        if get_active() == f"gwemu:{row['pid']}":
            set_active(None)
        return 130


def _gwemu_start(args) -> int:
    from gwprov.gwemu_manager import start_instance
    result = start_instance(args.profile, audio=args.audio, gdb_port=args.gdb_port,
                            headless=args.headless,
                            timeline=args.timeline, record_timeline=args.record_timeline,
                            timing_mode=args.timing_mode, icount=args.icount, rtc_epoch=args.rtc_epoch, gwemu_bin=args.gwemu_bin)
    if result == 0:
        from gwprov.active_device import set_active
        from gwprov.gwemu_manager import instances
        from gwprov.profiles import DeviceProfile
        profile_root = str(DeviceProfile.load(args.profile).root)
        row = next((item for item in instances() if item.get("profile") == profile_root), None)
        if row:
            set_active(f"gwemu:{row['pid']}")
    return result


def _gwemu_stop(args) -> int:
    from gwprov.gwemu_manager import instances, stop_instance
    from gwprov.active_device import get_active, set_active
    pid, profile = args.pid, args.profile
    if pid is None and profile is None:
        selected = _active_gwemu()
        pid, profile = selected.get("pid"), selected.get("profile")
    if pid is None and profile:
        from gwprov.profiles import resolve_profile_path
        selected = next((row for row in instances()
                         if row.get("profile") == str(resolve_profile_path(profile))), None)
        pid = selected.get("pid") if selected else None
    result = stop_instance(pid=pid, profile=profile, timeout=args.timeout)
    if result == 0 and pid is not None and get_active() == f"gwemu:{pid}":
        set_active(None)
    return result


def _active_gwemu():
    from gwprov.active_device import get_active
    from gwprov.gwemu_manager import instances
    active = get_active()
    if not active:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    if not active.startswith("gwemu:"):
        raise ValueError(f"active device {active!r} is not a GWemu instance")
    pid = int(active.split(":", 1)[1])
    match = next((row for row in instances() if row.get("pid") == pid), None)
    if match is None:
        raise ValueError(f"active GWemu instance {active} is no longer running; select another device")
    return match


def _active_gwemu_profile(profile: str | None) -> str:
    if profile:
        return profile
    selected = _active_gwemu()
    if not selected.get("profile"):
        raise ValueError("active GWemu instance has no managed profile; specify --profile")
    return selected["profile"]

def _gwemu_pause(args) -> int:
    from gwprov.gwemu_manager import set_instance_running
    profile, pid = args.profile, args.pid
    if profile is None and pid is None:
        selected = _active_gwemu()
        profile, pid = selected.get("profile"), selected.get("pid")
    return set_instance_running(profile, pid=pid, running=False)


def _gwemu_resume(args) -> int:
    from gwprov.gwemu_manager import set_instance_running
    profile, pid = args.profile, args.pid
    if profile is None and pid is None:
        selected = _active_gwemu()
        profile, pid = selected.get("profile"), selected.get("pid")
    return set_instance_running(profile, pid=pid, running=True)


def _gwemu_ps(args) -> int:
    from gwprov.gwemu_manager import show_instances
    return show_instances(output=args.output, no_pager=args.no_pager)


def _gwemu_screenshot(args) -> int:
    from gwprov.gwemu_manager import screenshot_instance
    pid, profile = args.pid, args.profile
    if pid is None and profile is None:
        selected = _active_gwemu()
        pid, profile = selected.get("pid"), selected.get("profile")
    return screenshot_instance(pid=pid, profile=profile, output=args.output)


def _gwemu_profile(args) -> int:
    from gwprov.profiling import profile_instance
    return profile_instance(_active_gwemu_profile(args.profile), duration=args.duration, interval=args.interval,
                            symbols=args.symbols, progress_symbols=args.progress_symbol,
                            rebase_symbols=args.rebase, output=args.output,
                            output_format=args.format, top=args.top, debug_config=args.debug_config,
                            stall_threshold=args.stall_threshold, progress_interval=args.progress_interval)


def _gwemu_diagnose(args) -> int:
    from gwprov.gwemu_manager import diagnose_instance
    return diagnose_instance(_active_gwemu_profile(args.profile), symbols=args.symbols,
                             output=args.output, max_frames=args.max_frames,
                             inspect_u32=args.u32, inspect_deref=args.deref,
                             inspect_bytes=args.bytes,
                             inspect_values=args.value, inspect_rings=args.ring, debug_config=args.debug_config)

def _gwemu_watch(args) -> int:
    from gwprov.gwemu_manager import watch_instance
    return watch_instance(_active_gwemu_profile(args.profile), symbols=args.symbols,
                          progress_symbols=args.progress_symbol,
                          guest_pc_symbols=args.guest_pc_symbol,
                          rebase_symbols=args.rebase,
                          watch_u32=args.u32, watch_deref=args.deref,
                          watch_bytes=args.bytes,
                          watch_values=args.value, watch_rings=args.ring,
                          heartbeat_symbol=args.heartbeat_symbol,
                          frame_symbol=args.frame_symbol,
                          interval=args.interval,
                          stall_after=args.stall_after, duration=args.duration,
                          output=args.output, debug_config=args.debug_config)


def _debug_gwemu(args) -> int:
    from gwprov.debug import debug_profile
    return debug_profile(_active_gwemu_profile(args.profile), gdb_port=args.gdb_port,
                         headless=args.headless,
                         qmp_socket=None, qmp_enabled=True,
                         symbols=args.symbols, gdb=args.gdb,
                         audio=args.audio, break_on_fault=not args.no_break_on_fault,
                         unpause_homebrew=args.unpause_homebrew,
                         app_symbols=args.app_symbols,
                         detach_after_app_entry=args.detach_after_app_entry,
                         keep_running=args.keep_running, timeline=args.timeline,
                         record_timeline=args.record_timeline)


def _ps(args) -> int:
    from gwprov.devices import show_devices
    return show_devices(output=args.output, profile=args.profile, no_pager=args.no_pager)


def _device_config(args) -> int:
    from gwprov.device_config import show_device_config
    return show_device_config(output=args.output)


def _device_recover(args) -> int:
    from gwprov.active_device import get_active, get_active_origin

    device_id = get_active()
    if not device_id:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    if device_id.startswith("probe:"):
        from gwprov.backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(device_id.removeprefix("probe:"),
                                       operation="gwprov device recover", lease_wait=0,
                                       allow_recovery=True)
    elif device_id.startswith("remote:"):
        from gwprov.backends import WebSocketBackend
        backend = WebSocketBackend(device_id.removeprefix("remote:"),
                                   origin=get_active_origin(),
                                   operation="gwprov device recover", lease_wait=0,
                                   allow_recovery=True)
    else:
        raise ValueError("device recover requires an active physical device")

    from gwprov.hw_lifecycle import recover_to_bank1
    try:
        backend.open()
        state = recover_to_bank1(backend)
    finally:
        backend.close()
    print("Device recovered: bank 1 is running")
    print(f"  PC: 0x{state['pc']:08x}  VTOR: 0x{state['vtor']:08x}")
    return 0


def _devices_command(args) -> int:
    if args.ids_only:
        from gwprov.devices import device_identifiers
        for identifier in device_identifiers():
            print(identifier)
        return 0
    return _resource_view("devices", args)


def _set_active(args) -> int:
    from gwprov.active_device import get_active, set_active
    from gwprov.devices import device_rows

    if args.device is None:
        if args.remote_origin:
            raise ValueError("--remote-origin requires selecting a remote device")
        active = get_active()
        print(f"Active device: {active or 'not set'}")
        return 0
    if args.device.startswith("remote:"):
        from gwprov.adapters import list_remote
        url = args.device.removeprefix("remote:")
        saved = next((row for row in list_remote() if row["url"] == url), None)
        origin = args.remote_origin if args.remote_origin is not None else (saved or {}).get("origin")
        set_active(args.device, origin=origin)
        print(f"Active device: {args.device} (remote gnwmanager)")
        return 0
    from gwprov.adapters import list_remote
    saved = next((row for row in list_remote() if row["name"].casefold() == args.device.casefold()), None)
    if saved:
        set_active(f"remote:{saved['url']}", origin=args.remote_origin or saved["origin"])
        print(f"Active device: {saved['name']} (remote gnwmanager)")
        return 0
    rows = device_rows()
    match = next((row for row in rows if row["id"] == args.device), None)
    if match is None:
        choices = ", ".join(row["id"] for row in rows) or "none detected"
        raise ValueError(f"unknown device {args.device!r}; detected devices: {choices}")
    set_active(match["id"], origin=args.remote_origin)
    print(f"Active device: {match['id']} ({match['name']})")
    return 0


def _set_assignment(args) -> int:
    from gwprov.active_device import get_active
    from gwprov.device_assignments import get_assignment, set_assignment
    device = get_active()
    if not device:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    field = "profile" if args.set_command == "profile" else "sdcard"
    if args.value is None:
        value = get_assignment(device).get(field)
        print(f"Active device {device} {field}: {value or 'not set'}")
        return 0
    if args.value.casefold() in {"none", "clear"}:
        set_assignment(device, field, None)
        print(f"Cleared {field} assignment for {device}")
        return 0
    if field == "profile":
        from gwprov.profiles import DeviceProfile
        value = str(DeviceProfile.load(args.value).root)
    else:
        if device.startswith("gwemu:"):
            raise ValueError("SD-card folders can be assigned to hardware devices; GWemu uses the profile's SD image")
        from gwprov.sd_cards import list_cards
        card = next((row for row in list_cards()
                     if row["name"].casefold() == args.value.casefold()), None)
        if card is None:
            choices = ", ".join(row["name"] for row in list_cards()) or "none registered"
            raise ValueError(f"unknown SD card {args.value!r}; registered cards: {choices}")
        value = card["name"]
    set_assignment(device, field, value)
    print(f"Assigned {field} {value} to {device}")
    return 0


def _sdcard(args) -> int:
    from gwprov.sd_cards import add_card, list_cards, remove_card
    if args.sdcard_command == "add":
        card = add_card(args.path, args.name)
        print(f"Added SD card {card['name']}: {card['path']}")
    elif args.sdcard_command == "remove":
        card = remove_card(args.name)
        print(f"Removed SD card {card['name']} ({card['path']})")
    else:
        rows = list_cards()
        if args.output == "names":
            for row in rows:
                print(row["name"])
        elif args.output == "json":
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No SD cards registered. Add a mounted card with `gwprov sdcard add PATH [NAME]`.")
        else:
            from rich.console import Console
            from rich.table import Table
            table = Table(title="SD cards", title_justify="left", expand=True)
            table.add_column("NAME", style="cyan")
            table.add_column("MOUNTED PATH", overflow="fold")
            for row in rows:
                table.add_row(row["name"], row["path"])
            Console().print(table)
    return 0


def _apply_assigned(args) -> int:
    output = getattr(args, "output", "text")
    from gwprov.active_device import get_active, get_active_origin, set_active
    from gwprov.device_assignments import get_assignment, set_assignment
    from gwprov.devices import device_rows
    from gwprov.profiles import DeviceProfile

    device_id = get_active()
    if not device_id:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    assignment = get_assignment(device_id)
    profile_path = assignment.get("profile")
    if not profile_path:
        raise ValueError(f"active device {device_id} has no assigned profile; use `gwprov set profile PROFILE`")
    profile = DeviceProfile.load(profile_path)
    rows = device_rows()
    row = next((item for item in rows if item["id"] == device_id), None)
    if row is None:
        raise ValueError(f"active device {device_id} is no longer available")

    if row["kind"] == "gwemu":
        from gwprov.gwemu_manager import stop_instance
        from gwprov.daemon import start_instance
        current_profile = row.get("profile")
        if current_profile == str(profile.root) and not assignment.get("sdcard"):
            if output == "json":
                print(json.dumps({"device": device_id, "profile": str(profile.root),
                                  "status": "already-applied"}, indent=2))
            else:
                print(f"GWemu pid {row['pid']} already uses profile {profile.root}.")
            return 0
        if row.get("status") == "unknown":
            raise ValueError("cannot apply to a GWemu instance with unknown execution state; inspect `gwprov ps`")
        old_id = device_id
        from gwprov.gwemu_manager import instances
        matches = [item for item in instances()
                   if item.get("profile") == str(profile.root)]
        newly_started = False
        if matches:
            new_id = f"gwemu:{matches[0]['pid']}"
        else:
            started = start_instance(str(profile.root), headless=row.get("display") == "headless")
            new_id = f"gwemu:{started['pid']}"
            newly_started = True
        try:
            stop_instance(pid=row["pid"])
        except Exception:
            if newly_started:
                try:
                    stop_instance(pid=started["pid"])
                except Exception:
                    pass
            raise
        set_assignment(old_id, "profile", None)
        set_assignment(new_id, "profile", str(profile.root))
        if assignment.get("sdcard"):
            set_assignment(new_id, "sdcard", assignment["sdcard"])
        set_active(new_id)
        if output == "json":
            print(json.dumps({"device": new_id, "profile": str(profile.root),
                              "status": "applied"}, indent=2))
        else:
            print(f"Applied profile {profile.root} to GWemu; active device is {new_id}.")
        return 0

    target = {}
    if row.get("probeId"):
        target["probe_id"] = row["probeId"]
    elif device_id.startswith("remote:"):
        target["remote_url"] = device_id.removeprefix("remote:")
        target["remote_origin"] = get_active_origin()
    else:
        raise ValueError(f"active device {device_id} cannot receive a deployment")

    regions = None
    sd_written = None
    card_name = assignment.get("sdcard")
    if card_name and profile.resolved_sd:
        from gwprov.sd_cards import list_cards
        card = next((item for item in list_cards()
                     if item["name"].casefold() == card_name.casefold()), None)
        if card is None:
            raise ValueError(f"assigned SD card {card_name!r} is no longer registered")
        from gwprov.deploy import deployment_plan, overlay_sd_directory
        planned_regions = deployment_plan(str(profile.root))["regions"]
        regions = [item["region"] for item in planned_regions if item["region"] != "sd"]
        sd_written = overlay_sd_directory(profile.resolved_sd, Path(card["path"]))
    if regions == []:
        result = {"profile": str(profile.root), "written": [
            {"region": "sd", "files": sd_written, "mode": "mounted-folder-overlay"}
        ]}
        if output == "json":
            print(json.dumps(result, indent=2))
        else:
            print(f"Updated SD card with {sd_written} file(s).", flush=True)
        return 0
    from gwprov.deploy import apply_deployment
    callback, finish = _deployment_progress_display(output)
    try:
        result = apply_deployment(str(profile.root), regions=regions,
                                  progress=callback, **target)
    finally:
        if finish:
            finish()
    if sd_written is not None:
        result["written"].append({"region": "sd", "files": sd_written,
                                  "mode": "mounted-folder-overlay"})
    if output == "json":
        print(json.dumps(result, indent=2))
    return 0


def _apply_parser(args) -> int:
    return _apply_assigned(args)


def _resource_view(section: str, args) -> int:
    args.section = section
    if not hasattr(args, "firmware_repo"):
        args.firmware_repo = "sylverb/game-and-watch-retro-go-sd"
    return _show(args)


def _adapters_list(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from gwprov.active_device import get_active
    from gwprov.adapters import inventory

    rows = inventory()
    active = get_active()
    if args.output == "json":
        print(json.dumps([{**row, "active": row["id"] == active} for row in rows], indent=2))
        return 0
    if not rows:
        Console().print("[dim]No local adapters or registered remote adapters found. Use `gwprov adapters add NAME URL` to register a remote server.[/]")
        return 0
    table = Table(title="Adapters", title_justify="left", expand=True)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("ADAPTER", style="cyan", overflow="fold")
    table.add_column("TYPE", overflow="fold")
    table.add_column("ID", overflow="fold")
    table.add_column("STATE")
    for row in rows:
        table.add_row("●" if row["id"] == active else "", row["name"], row["adapter"],
                      row["id"], row["state"].upper())
    Console().print(table)
    if active:
        print("● Active device")
    return 0


def _adapters_add(args) -> int:
    from gwprov.adapters import add_remote
    from gwprov.active_device import get_active, set_active
    adapter = add_remote(args.name, args.url, origin=args.origin)
    device_id = f"remote:{adapter['url']}"
    if get_active() == device_id:
        set_active(device_id, origin=adapter["origin"])
    print(f"Added remote adapter {adapter['name']}: {adapter['url']}")
    if adapter["origin"]:
        print(f"  Origin: {adapter['origin']}")
    print(f"Select its device with: gwprov set active {adapter['name']}")
    return 0


def _adapters_remove(args) -> int:
    from gwprov.adapters import remove_remote
    from gwprov.active_device import get_active, set_active
    adapter = remove_remote(args.name_or_url)
    device_id = f"remote:{adapter['url']}"
    if get_active() == device_id:
        set_active(None)
    print(f"Removed remote adapter {adapter['name']} ({adapter['url']})")
    return 0


def _performance_report_paths() -> list[str]:
    from gwprov.profiles import profile_directory
    roots = [Path("dev-local/reports")]
    profile_root = profile_directory()
    if profile_root.exists():
        roots.extend(path / "runtime/gwprov" for path in profile_root.iterdir()
                     if path.is_dir())
    extensions = {".json", ".html", ".pdf"}
    return sorted({str(path) for root in roots if root.exists()
                   for path in root.rglob("*")
                   if path.is_file() and path.suffix.casefold() in extensions})


def _show(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    from gwprov.active_device import get_active
    from gwprov.devices import device_rows
    section = getattr(args, "section", None)
    if section == "devices":
        rows = device_rows()
        active = get_active()
        if args.output == "json":
            print(json.dumps([{**{k: row.get(k) for k in ("id", "kind", "name", "status", "application",
                                                              "assignedProfile", "sdCard")},
                               "active": row["id"] == active} for row in rows], indent=2))
            return 0
        if not rows:
            Console().print("[dim]No devices detected. Connect a probe or start GWemu, then run `gwprov show devices` again.[/]")
            return 0
        table = Table(title="Devices", title_justify="left", expand=True)
        table.add_column("", width=1, no_wrap=True)
        for name in ("DEVICE", "TYPE", "STATE", "APPLICATION", "PROFILE", "SD CARD"):
            table.add_column(name, overflow="fold")
        for row in rows:
            state = Text(str(row["status"]).upper(), style={"running": "green", "halted": "yellow",
                         "busy": "cyan", "unknown": "red"}.get(row["status"], "white"))
            table.add_row(Text("●" if row["id"] == active else "", style="bold cyan"),
                          row["id"], row["kind"], state, row.get("application", "Unknown"),
                          row.get("assignedProfile", ""), row.get("sdCard", ""))
        Console().print(table)
        if active:
            print("● Active device")
        return 0
    if section == "adapters":
        from gwprov.adapters import inventory
        adapters = {}
        for row in inventory():
            key = row["adapter"]
            adapters.setdefault(key, []).append(row)
        data = [{"adapter": name, "count": len(items), "state": ", ".join(sorted({r["state"] for r in items}))}
                for name, items in sorted(adapters.items())]
        if args.output == "json":
            print(json.dumps(data, indent=2))
        else:
            if not data:
                Console().print("[dim]No local or registered remote adapters detected.[/]")
                return 0
            table = Table(title="Adapters", title_justify="left", expand=True)
            table.add_column("ADAPTER", style="cyan")
            table.add_column("COUNT", justify="right")
            table.add_column("STATE")
            for item in data:
                table.add_row(item["adapter"], str(item["count"]), item["state"])
            Console().print(table)
        return 0
    if section == "projects":
        from gwprov.dist import load_project_catalog
        catalog = load_project_catalog(getattr(args, "firmware_repo", "sylverb/game-and-watch-retro-go-sd"))
        projects = catalog["projects"]
        if args.output == "json":
            print(json.dumps(projects, indent=2))
        else:
            print(f"{len(projects)} projects in catalog ({sum(p['kind'] == 'core' for p in projects)} cores, "
                  f"{sum(p['kind'] == 'homebrew' for p in projects)} homebrew)")
            print(f"Browse details with: gwprov projects list")
        return 0
    if section == "profile":
        from gwprov.profiles import list_profiles
        profiles = list_profiles()
        if args.output == "json":
            print(json.dumps(profiles, indent=2))
        else:
            if not profiles:
                print("No device profiles found. Create one with `gwprov profile create`.")
                return 0
            print(f"{len(profiles)} device profile(s)")
            for profile in profiles:
                print(f"  {profile['name']:<20} {profile['status']:<10} {profile['display_name']}")
            print("Manage profiles with: gwprov profile create|duplicate|list|show")
        return 0
    if section == "sdcard":
        from gwprov.sd_cards import list_cards
        cards = list_cards()
        if args.output == "json":
            print(json.dumps(cards, indent=2))
        elif not cards:
            print("No SD cards registered. Add a mounted card with `gwprov sdcard add PATH [NAME]`.")
        else:
            print(f"{len(cards)} SD card(s) registered")
            for card in cards:
                print(f"  {card['name']:<16} {card['path']}")
        return 0
    if section in ("perf", "performance"):
        reports = _performance_report_paths()
        if args.output == "json":
            print(json.dumps(reports, indent=2))
        else:
            if not reports:
                print("No performance reports found yet.")
                return 0
            print(f"{len(reports)} performance report(s) found")
            for report in reports:
                print(f"  {report}")
        return 0
    if args.output == "json":
        rows = device_rows()
        from gwprov.profiles import list_profiles
        profiles = list_profiles()
        from gwprov.adapters import inventory
        adapter_rows = inventory()
        from gwprov.sd_cards import list_cards
        print(json.dumps({"devices": len(rows), "adapters": len(adapter_rows),
                          "projects": "available via project list", "profiles": len(profiles),
                          "sdCards": len(list_cards()),
                          "performanceProfiles": len(_performance_report_paths()),
                          "active": get_active()}, indent=2))
    else:
        rows = device_rows()
        from gwprov.profiles import list_profiles
        profiles = list_profiles()
        print("GWProv manages")
        vm_count = sum(row["kind"] == "gwemu" for row in rows)
        hardware_count = sum(row["kind"] == "hardware" for row in rows)
        print(f"  Devices       {len(rows)} detected ({vm_count} GWemu, {hardware_count} hardware)")
        from gwprov.adapters import inventory
        adapter_rows = inventory()
        from gwprov.sd_cards import list_cards
        cards = list_cards()
        print(f"  Adapters      {len(adapter_rows)} detected")
        print("  Projects      Retro-Go cores and homebrew")
        print(f"  Profiles      {len(profiles)} device profile(s)")
        print(f"  SD cards      {len(cards)} mounted card(s) registered")
        print(f"  Perf          {len(_performance_report_paths())} saved report(s)")
        print(f"  Active        {get_active() or 'not set'}")
        print("Use `gwprov show devices|adapters|projects|profile|perf|sdcard` for an overview.")
    return 0


def _deploy_plan(args) -> int:
    from gwprov.deploy import deployment_plan
    plan = deployment_plan(args.profile, args.region)
    if args.output == "json":
        print(json.dumps(plan, indent=2))
    else:
        print(f"Deployment plan: {plan['profile']}")
        for row in plan["regions"]:
            destination = f"bank {row['bank']}+0x{row['offset']:x}" if "bank" in row else f"offset 0x{row['offset']:x}"
            print(f"  {row['region']:<10} {row['bytes']:>10} bytes  {destination}  sha256={row['sha256']}")
        print("Boot after deployment: bank 1 reset vector")
        print("SD deployment overlays files and retains existing files.")
    return 0


def _deployment_progress_display(output: str):
    """Return a stdout callback for readable deployment progress."""
    if output == "json":
        return None, None
    from rich.progress import (BarColumn, DownloadColumn, Progress, TaskProgressColumn,
                               TextColumn, TimeElapsedColumn)
    console = Console(file=sys.stdout, force_terminal=sys.stdout.isatty())
    if sys.stdout.isatty():
        progress = Progress(TextColumn("{task.description}"), BarColumn(),
                            TaskProgressColumn(), DownloadColumn(), TimeElapsedColumn(),
                            console=console)
        progress.start()
        task_id = None

        def update(event: dict) -> None:
            nonlocal task_id
            kind = event["event"]
            if kind == "start":
                task_id = progress.add_task("Preparing deployment", total=event["total_bytes"])
            elif kind == "phase" and task_id is not None:
                progress.update(task_id, description=event["message"])
            elif kind == "region_start" and task_id is not None:
                progress.update(task_id, description=f"Writing {event['region']}")
            elif kind == "progress" and task_id is not None:
                progress.update(task_id, completed=event["overall_done"])
            elif kind == "complete" and task_id is not None:
                progress.update(task_id, completed=event["overall_total"],
                                description="Deployment complete")

        def finish() -> None:
            progress.stop()
        return update, finish

    last_percent = -1

    def update(event: dict) -> None:
        nonlocal last_percent
        kind = event["event"]
        if kind == "start":
            print(f"Preparing deployment ({event['region_count']} regions, "
                  f"{event['total_bytes'] / (1024 * 1024):.1f} MiB).", flush=True)
        elif kind == "phase":
            print(f"{event['message']}…", flush=True)
        elif kind == "region_start":
            print(f"Writing {event['region']} ({event['bytes'] / 1024:.0f} KiB).", flush=True)
            last_percent = -1
        elif kind == "progress":
            total = event["region_total"]
            percent = 100 if total == 0 else event["region_done"] * 100 // total
            if percent == 100 or percent >= last_percent + 10:
                print(f"  {event['region']}: {percent}% "
                      f"({event['region_done'] / (1024 * 1024):.1f}/"
                      f"{total / (1024 * 1024):.1f} MiB)", flush=True)
                last_percent = percent
        elif kind == "region_complete":
            print(f"  {event['region']}: complete.", flush=True)
        elif kind == "complete":
            print("Deployment complete; booted bank 1.", flush=True)

    return update, None


def _deploy_apply(args) -> int:
    from gwprov.deploy import apply_deployment
    probe_id, programmer, remote_url = args.probe_id, args.programmer, args.remote_url
    remote_origin = args.remote_origin
    if not any((probe_id, programmer, remote_url)):
        from gwprov.active_device import get_active
        from gwprov.devices import device_rows
        active = get_active()
        row = next((item for item in device_rows() if item["id"] == active), None) if active else None
        if row is None:
            raise ValueError("select a physical device with `gwprov set active DEVICE` or specify a target")
        if row.get("probeId"):
            probe_id = row["probeId"]
        elif row["id"].startswith("remote:"):
            remote_url = row["id"].removeprefix("remote:")
            from gwprov.active_device import get_active_origin
            remote_origin = remote_origin or get_active_origin()
        else:
            raise ValueError("deployment requires a physical hardware device")
    callback, finish = _deployment_progress_display(args.output)
    try:
        result = apply_deployment(args.profile, probe_id=probe_id, programmer=programmer,
                                  remote_url=remote_url, remote_origin=remote_origin,
                                  regions=args.region, progress=callback)
    finally:
        if finish:
            finish()
    if args.output == "json":
        print(json.dumps(result, indent=2))
    return 0


def _profile_hardware(args) -> int:
    from gwprov.hw_profile import profile_hardware
    probe_id, programmer, remote_url = args.probe_id, args.programmer, args.remote_url
    remote_origin = args.remote_origin
    if not any((probe_id, programmer, remote_url)):
        from gwprov.active_device import get_active
        from gwprov.devices import device_rows
        selected_id = get_active()
        row = next((item for item in device_rows() if item["id"] == selected_id), None) if selected_id else None
        if row is None:
            raise ValueError("select a physical device with `gwprov set active DEVICE` or specify a target")
        if row.get("probeId"):
            probe_id = row["probeId"]
        elif row["id"].startswith("remote:"):
            remote_url = row["id"].removeprefix("remote:")
            from gwprov.active_device import get_active_origin
            remote_origin = remote_origin or get_active_origin()
        else:
            raise ValueError("active device is not a hardware target")
    return profile_hardware(probe_id=probe_id, programmer=programmer,
                            remote_url=remote_url,
                            remote_origin=remote_origin, profile=args.profile,
                            symbols=args.symbols, duration=args.duration,
                            interval=args.interval, output=args.output,
                            output_format=args.format, top=args.top,
                            counter_symbols=args.counter_symbol,
                            frame_counter=args.frame_counter,
                            total_cycle_counter=args.total_cycle_counter,
                            start_at=args.start_at,
                            set_registers=args.set_register,
                            startup_timeout=args.startup_timeout)


def _add_hardware_perf_command(subparsers, *, name: str = "hardware", help_text: str,
                              hidden: bool = False):
    command = subparsers.add_parser(name, help=help_text)
    command._gwprov_hidden = hidden
    target = command.add_mutually_exclusive_group()
    target.add_argument("--probe-id", help="select one local PyOCD probe by unique ID")
    target.add_argument("--programmer", choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                        help="select one local OpenOCD adapter explicitly")
    target.add_argument("--remote-url", help="use one gnwmanager serve URL")
    command.add_argument("--remote-origin", help="Origin required by the selected remote server")
    command.add_argument("--profile", help="load firmware and app symbols from a device profile")
    command.add_argument("--symbols", action="append", default=[], metavar="ELF",
                         help="additional firmware/app ELF symbols")
    command.add_argument("--counter-symbol", action="append", default=[], metavar="SYMBOL",
                         help="capture a sized integer counter from the loaded ELF; repeatable")
    command.add_argument("--frame-counter", help="counter symbol whose delta is reported as logical FPS")
    command.add_argument("--total-cycle-counter",
                         help="cycle counter used to calculate percentages for *_cycles counters")
    command.add_argument("--start-at", metavar="SYMBOL",
                         help="reset hardware and begin capture at this function entry (local PyOCD only)")
    command.add_argument("--set-register", action="append", default=[], metavar="REG=VALUE",
                         help="write a core register at --start-at before resuming; repeatable")
    command.add_argument("--startup-timeout", type=float, default=120.0,
                         help="maximum seconds to reach --start-at after reset")
    command.add_argument("--duration", type=float, default=10.0)
    command.add_argument("--interval", type=float, default=0.05)
    command.add_argument("--output", help="report path; default under dev-local/reports")
    command.add_argument("--format", choices=("text", "json", "html", "pdf"), default="text")
    command.add_argument("--top", type=int, default=10)
    command.set_defaults(handler=_profile_hardware)
    return command


def _report_render(args) -> int:
    from gwprov.reports import render_report
    output = render_report(args.input, args.output, format=args.format)
    print(f"Rendered {args.format.upper()} report: {output}")
    return 0


def _debug_python(args) -> int:
    from gwprov.debug_shell import python_shell

    symbol_paths = list(args.symbols)
    qmp_socket = args.qmp_socket
    profile = args.profile
    probe_ids = list(args.probe_id)
    programmers = list(args.programmer)
    remote_urls = list(args.remote_url)
    remote_origins = list(args.remote_origin)
    if args.target == "hardware" and not any((probe_ids, programmers, remote_urls)):
        from gwprov.active_device import get_active
        from gwprov.devices import device_rows
        selected_id = get_active()
        row = next((item for item in device_rows() if item["id"] == selected_id), None) if selected_id else None
        if row is None:
            raise ValueError("select a physical device with `gwprov set active DEVICE` or specify a target")
        if row.get("probeId"):
            probe_ids.append(row["probeId"])
        elif row["id"].startswith("remote:"):
            remote_urls.append(row["id"].removeprefix("remote:"))
            from gwprov.active_device import get_active_origin
            origin = get_active_origin()
            if origin:
                remote_origins.append(origin)
        else:
            raise ValueError("active device is not a hardware target")
    if args.target == "gwemu" and qmp_socket is None and not profile:
        from gwprov.active_device import get_active
        if get_active():
            selected = _active_gwemu()
            qmp_socket = selected.get("qmpSocket")
            profile = selected.get("profile")
            if qmp_socket is None:
                raise ValueError("active GWemu device has no QMP endpoint")
    if profile:
        from gwprov.profiles import DeviceProfile
        profile_root = DeviceProfile.load(profile).root
        firmware_symbols = profile_root / "debug" / "retro-go-debug.elf"
        if not firmware_symbols.is_file():
            raise ValueError(f"profile has no bundled firmware symbols: {firmware_symbols}")
        symbol_paths.insert(0, str(firmware_symbols))
        app_symbols = sorted((firmware_symbols.parent / "apps").rglob("*.elf"))
        symbol_paths[1:1] = [str(path) for path in app_symbols]
        if args.target == "gwemu" and qmp_socket is None:
            from gwprov.gwemu_manager import instances
            matches = [row for row in instances() if row.get("profile") == str(profile_root)]
            if len(matches) == 1:
                qmp_socket = matches[0].get("qmpSocket")
            elif len(matches) > 1:
                raise ValueError("profile has multiple GWemu instances; select a QMP handle explicitly")
    return python_shell(target=args.target, host=args.host, port=args.port,
                        openocd_port=args.openocd_port, probe_ids=probe_ids,
                        programmers=programmers, remote_urls=remote_urls,
                        remote_origins=remote_origins,
                        symbols=symbol_paths,
                        qmp_socket=qmp_socket, debug_config=args.debug_config)


def _ofw_patch(args) -> int:
    from gwprov.vendor.qemu_gnw import cfw_images

    forwarded = [
        args.game,
        "--source-tree", args.source_tree,
        "--backup-dir", args.backup_dir,
        "--output-dir", args.output_dir,
        *args.forwarded,
    ]
    cfw_images.main(forwarded)
    return 0


def _fs_pack(args) -> int:
    from gwprov.vendor.retrogo_sd.scripts import gen_frogfs_image, gen_littlefs_image

    module = gen_frogfs_image if args.filesystem == "frogfs" else gen_littlefs_image
    forwarded = ["--retro-go-root", args.retro_go_root, *args.forwarded]
    result = module.main(forwarded)
    return int(result or 0)


def _sd_create(args) -> int:
    from gwprov.common.sdcard import create_image

    output = Path(args.image).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    create_image(str(output), size_mb=args.size_mb, label=args.label)
    return 0


def _sd_compose(args) -> int:
    from gwprov.common.sdcard import QemuSDCardManager, compose

    manager = QemuSDCardManager(str(Path(args.image).expanduser().resolve()))
    compose(
        manager,
        core_bin=args.core,
        core_name=args.core_name,
        rom=args.rom,
        rom_dir=args.rom_dir,
        config=args.config,
        content_dir=args.content_dir,
    )
    return 0


def _input_tap(args) -> int:
    from gwprov.remote_input import NAMES, ShadowCellTransport, session

    names = [part.strip().upper() for part in args.buttons.split("+")]
    unknown = [name for name in names if name not in NAMES]
    if unknown:
        raise ValueError(f"unknown button(s): {', '.join(unknown)}")
    keys = [NAMES[name] for name in names]
    transport = None
    from gwprov.active_device import get_active
    active = get_active()
    explicit = any((args.probe_id, args.programmer, args.remote_url))
    backend = None
    if active and not explicit:
        from gwprov.devices import device_rows
        row = next((item for item in device_rows() if item["id"] == active), None)
        if row is None:
            raise ValueError(f"active device {active!r} is no longer available")
        if row.get("probeId"):
            from gwprov.backends import SelectedPyOCDBackend
            from gwprov.target_leases import adapter_for_probe
            backend = SelectedPyOCDBackend(row["probeId"], operation="gwprov input transport",
                                           probe_adapter=adapter_for_probe(
                                               f"{row.get('vendor', '')} {row.get('name', '')}"))
        elif row["id"].startswith("remote:"):
            from gwprov.backends import WebSocketBackend
            from gwprov.active_device import get_active_origin
            backend = WebSocketBackend(row["id"].removeprefix("remote:"),
                                       origin=get_active_origin(),
                                       operation="gwprov input transport")
        else:
            raise ValueError("input tap requires a physical hardware device to be active")
    elif args.probe_id:
        from gwprov.backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(args.probe_id, operation="gwprov input transport")
    elif args.programmer:
        from gwprov.backends import SelectedOpenOCDBackend
        backend = SelectedOpenOCDBackend(args.programmer, operation="gwprov input transport")
    elif args.remote_url:
        from gwprov.backends import WebSocketBackend
        backend = WebSocketBackend(args.remote_url, origin=args.remote_origin,
                                   operation="gwprov input transport")
    elif not active:
        raise ValueError("no active device; select one with `gwprov set active DEVICE` or specify a target")
    if backend is not None:
        backend.open()
        transport = ShadowCellTransport(backend=backend)
    try:
        with session(transport=transport) as dev:
            dev.tap(keys, repeat=args.repeat, tap_ms=args.tap_ms, gap_ms=args.gap_ms)
    finally:
        if backend is not None:
            backend.close()
    return 0


def _media_inventory(args) -> int:
    from gwprov.inventory import inspect_profile, frogfs, littlefs, fatfs
    if args.profile:
        result = inspect_profile(args.profile, args.shared_sd_root)
    else:
        image = Path(args.image).expanduser().resolve()
        if not args.filesystem:
            raise ValueError('--image requires --filesystem')
        if args.filesystem == 'littlefs':
            if not args.size:
                raise ValueError('LittleFS inventory requires --size')
            entry = littlefs(image, args.offset, args.size, args.block_size)
        else:
            entry = (frogfs if args.filesystem == 'frogfs' else fatfs)(image, args.offset)
        result = {'schemaVersion': 1, 'images': {}, 'filesystems': [entry]}
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")
    return 0


def _filesystem(args) -> int:
    from gwprov.filesystem import operate
    operation = "ls" if args.fs_command in ("ls", "tree") else "delete" if args.fs_command in ("delete", "del", "remove", "rm") else args.fs_command
    target = "sd" if args.target == "sdcard" else args.target
    filesystem = getattr(args, "filesystem", None)
    image = args.image
    size = args.size
    size_mib = getattr(args, "size_mib", None)
    if args.fs_command == "create":
        aliases = {"littlefs": "littlefs", "lfs": "littlefs",
                   "frogfs": "frogfs", "sd": "sd", "sdcard": "sd"}
        filesystem = aliases.get(getattr(args, "fs_type", None), filesystem)
        if size_mib is None:
            size_mib = getattr(args, "size_mib_pos", None)
        if image is None and not args.profile:
            directory = Path(getattr(args, "target_dir", None) or ".").expanduser()
            filename = getattr(args, "filename", None)
            filename = filename or {"frogfs": "frogfs.bin", "littlefs": "lfs.bin",
                                    "sd": "sdcard.bin"}.get(filesystem)
            if not filename:
                raise ValueError("choose frogfs, littlefs/lfs, or sdcard/sd")
            image = str(directory / filename)
        if size is None and size_mib is not None and filesystem != "sd":
            size = size_mib * 1024 * 1024
    return operate(operation=operation, target=target, profile=args.profile,
                   image=image, filesystem=filesystem, offset=args.offset,
                   size=size, block_size=args.block_size,
                   path=getattr(args, "destination", None) or getattr(args, "path", None) or "/",
                   source=getattr(args, "source", None), size_mib=size_mib,
                   force=getattr(args, "force", False))


def _media_compare(args) -> int:
    from gwprov.inventory import compare
    result = compare(json.loads(Path(args.expected).read_text()),
                     json.loads(Path(args.actual).read_text()), args.mode)
    print(json.dumps(result, indent=2))
    return 0 if result["equal"] else 1


def _profile_show(args) -> int:
    from gwprov.profiles import DeviceProfile

    profile = DeviceProfile.load(args.directory, shared_sd_root=args.shared_sd_root)
    result = {
        "display_name": profile.display_name,
        "root": str(profile.root),
        "bank1": str(profile.bank1),
        "bank2": str(profile.bank2),
        "extflash": str(profile.extflash),
        "sd_mode": profile.sd_mode,
        "sd_image": str(profile.resolved_sd or ""),
        "provenance": profile.provenance,
    }
    print(json.dumps(result, indent=2))
    return 0



def _project_list(args) -> int:
    from gwprov.dist import load_project_catalog

    catalog = load_project_catalog(args.firmware_repo)
    projects = catalog["projects"]
    if args.name:
        name = args.name.strip().casefold().removesuffix(".git")
        from gwprov.dist.project import _catalog_entry

        item = _catalog_entry(name, catalog)
        projects = [item] if item else []
    if args.output == "json":
        print(json.dumps({**catalog, "projects": projects}, indent=2))
        return 0
    if args.name and not projects:
        raise ValueError(f"no curated project matches {args.name!r}")
    for kind in ("core", "homebrew"):
        entries = [item for item in projects if item["kind"] == kind]
        if not entries:
            continue
        label = "Cores" if kind == "core" else "Homebrew"
        print(f"{label} ({len(entries)})")
        for item in entries:
            print(f"  {item['project']:<16} {item['title']}")
    return 0


def _project_versions(args) -> int:
    from gwprov.dist import list_versions
    from gwprov.dist.project import _project_reference

    name, versions_url = _project_reference(args.repo)
    index = list_versions(name, versions_url=versions_url)
    if args.output == "json":
        print(json.dumps(index, indent=2))
    else:
        print(f"{index.get('title', name)} ({index['repo']})")
        for version in index["versions"]:
            if isinstance(version, dict):
                suffixes = []
                if version.get("kind"):
                    suffixes.append(version["kind"])
                if version.get("needsUserFiles"):
                    suffixes.append("needs user files")
                print(f"  {version.get('tag', '?')}  {version.get('publishedAt', '')}" +
                      (f"  ({', '.join(suffixes)})" if suffixes else ""))
    return 0


def _project_info(args) -> int:
    from gwprov.dist import resolve_project
    from gwprov.dist.project import _project_reference

    name, versions_url = _project_reference(args.repo)
    resolved = resolve_project(name, args.version, versions_url=versions_url)
    manifest = resolved.manifest
    result = {
        "repo": resolved.repo,
        "version": resolved.version.get("tag"),
        "title": manifest.get("title"),
        "storage": manifest.get("storage"),
        "targets": [],
    }
    for target in manifest.get("targets", []):
        result["targets"].append({
            "id": target.get("id"),
            "kind": target.get("kind"),
            "label": target.get("label"),
            "systems": [
                {"id": system.get("id"), "firmware": [
                    {"id": slot.get("id"), "filename": slot.get("filename"),
                     "required": slot.get("required", False), "requiredFor": slot.get("requiredFor", [])}
                    for slot in system.get("firmware", system.get("bios", []))
                ]}
                for system in target.get("systems", [])
            ],
            "tools": [use.get("tool") for use in target.get("uses", [])],
        })
    if args.output == "json":
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['title']}  ({result['repo']} @ {result['version']})")
    if result["storage"]:
        print(f"Storage: {', '.join(result['storage'])}")
    for target in result["targets"]:
        print(f"{target['kind']}: {target['id']} — {target['label']}")
        for system in target["systems"]:
            print(f"  System: {system['id']}")
            for firmware in system["firmware"]:
                required = "required" if firmware["required"] else "optional"
                filename = firmware["filename"]
                if isinstance(filename, list):
                    filename = ", ".join(filename)
                print(f"    Firmware ({required}): {firmware['id']} — {filename}")
        if target["tools"]:
            print(f"  Converters: {', '.join(target['tools'])}")
    return 0


def _project_install(args) -> int:
    from gwprov.dist import install_project
    from gwprov.dist.project import _project_reference

    name, versions_url = _project_reference(args.repo)
    result = install_project(
        name,
        output=args.output,
        variant=args.variant,
        versions_url=versions_url,
        target_id=args.target,
        tag=args.version,
        input_files=args.input,
        input_dirs=args.input_dir,
        firmware_files=args.firmware,
        firmware_dirs=args.firmware_dir,
        game_files=args.game,
        game_dirs=args.game_dir,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2))
    return 0


def _completion_bash(args) -> int:
    print("""_gwprov_project_names() {
  if [[ ! ${_GWPROV_PROJECT_NAMES+x} ]]; then
    local catalog
    catalog=$(gwprov projects list --output json 2>/dev/null) || return
    _GWPROV_PROJECT_NAMES=$(python3 -c 'import json,sys,urllib.parse; projects=json.load(sys.stdin)["projects"]; [(print(p["project"]), print((urllib.parse.urlparse(p["versionsUrl"]).hostname or "").split(".",1)[0]+"/"+p["project"])) for p in projects]' <<< "$catalog")
  fi
  printf '%s\n' "$_GWPROV_PROJECT_NAMES"
}
_gwprov_profile_names() { gwprov profile list --output names 2>/dev/null; }
_gwprov_device_names() { gwprov devices --ids-only 2>/dev/null; }
_gwprov_sdcard_names() { gwprov sdcard list --output names 2>/dev/null; }
_gwprov_complete_profiles() {
  local token="$1" prefix="" value="$1"
  if [[ "$token" == *=* ]]; then prefix="${token%%=*}="; value="${token#*=}"; fi
  if [[ "$value" == */* || "$value" == ~* ]]; then
    _gwprov_complete_dirs "$token" directories
  else
    COMPREPLY=( $(compgen -W "$(_gwprov_profile_names)" -- "$value") )
    local index
    for index in "${!COMPREPLY[@]}"; do COMPREPLY[$index]="${prefix}${COMPREPLY[$index]}"; done
  fi
}
_gwprov_complete_dirs() {
  local token="$1" mode="${2:-all}" prefix="" pathpart search match completed
  if [[ "$token" == *=* ]]; then
    prefix="${token%%=*}="
    pathpart="${token#*=}"
  else
    pathpart="$token"
  fi
  if [[ "$pathpart" == "~" ]]; then
    search="$HOME/"
  elif [[ "$pathpart" == "~/"* ]]; then
    search="$HOME/${pathpart:2}"
  else
    search="${pathpart:-./}"
  fi
  COMPREPLY=()
  compopt -o filenames -o nospace 2>/dev/null
  while IFS= read -r match; do
    if [[ "$pathpart" == "~"* && "$match" == "$HOME/"* ]]; then
      completed="~/${match#"$HOME/"}"
    elif [[ "$pathpart" == "~" && "$match" == "$HOME" ]]; then
      completed="~"
    else
      completed="$match"
    fi
    if [[ -d "$match" ]]; then completed="${completed%/}/"; fi
    COMPREPLY+=("${prefix}${completed}")
  done < <(if [[ "$mode" == directories ]]; then compgen -d -- "$search"; else compgen -f -- "$search"; fi)
}
_gwprov_complete() {
  local cur prev context candidates extra_candidates candidate
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev=""
  if (( COMP_CWORD > 0 )); then prev="${COMP_WORDS[COMP_CWORD-1]-}"; fi
  context="${COMP_WORDS[1]-}:${COMP_WORDS[2]-}"
  extra_candidates=""
  case "$cur" in
    --profile=*) _gwprov_complete_profiles "$cur"; return ;;
    --input=*|--input-dir=*|--firmware=*|--bios=*|--firmware-dir=*|--bios-dir=*|--game=*|--game-dir=*|--content=*|--source=*|--profile=*|--output-dir=*|--bootloader-file=*|--backup-dir=*|--source-tree=*|--image=*|--shared-sd-root=*|--retro-go-root=*|--rom=*|--rom-dir=*|--config=*|--timeline=*|--record-timeline=*|--bank1=*|--bank2=*|--extflash=*|--sdcard=*|--gwemu-bin=*)
      _gwprov_complete_dirs "$cur"
      return
      ;;
    --variant=*)
      COMPREPLY=()
      while IFS= read -r candidate; do COMPREPLY+=("--variant=$candidate"); done < <(compgen -W "flash sd" -- "${cur#*=}")
      return
      ;;
    --format=*)
      local format_values
      case "$context" in
        tree:*) format_values="tree names" ;;
        gwemu:profile) format_values="text json" ;;
        profile:hardware|perf:hardware) format_values="text json html pdf" ;;
        report:render) format_values="html pdf" ;;
        *) format_values="" ;;
      esac
      COMPREPLY=()
      while IFS= read -r candidate; do COMPREPLY+=("--format=$candidate"); done < <(compgen -W "$format_values" -- "${cur#*=}")
      return
      ;;
    --output=*)
      local output_values
      case "$context" in
        ps:*|apply:|deploy:plan|deploy:apply|projects:list|project:list|projects:versions|project:versions|projects:info|project:info|devices|adapters:list|gwemu:ps) output_values="text json" ;;
        profile:list|sdcard:list) output_values="text json names" ;;
        profile:duplicate) output_values="text json" ;;
        *) output_values="" ;;
      esac
      if [[ -n "$output_values" ]]; then
        COMPREPLY=()
        while IFS= read -r candidate; do COMPREPLY+=("--output=$candidate"); done < <(compgen -W "$output_values" -- "${cur#*=}")
        return
      fi
      _gwprov_complete_dirs "$cur"
      return
      ;;
  esac
  if [[ "$prev" == "--variant" ]]; then
    COMPREPLY=( $(compgen -W "flash sd" -- "$cur") )
    return
  fi
  if [[ "$prev" == "--profile" ]]; then _gwprov_complete_profiles "$cur"; return; fi
  case "$prev:$context" in
    --format:tree:*) candidates="tree names" ;;
    --format:gwemu:profile) candidates="text json" ;;
    --format:profile:hardware|--format:perf:hardware) candidates="text json html pdf" ;;
    --format:report:render) candidates="html pdf" ;;
    --output:ps:*|--output:apply:|--output:deploy:plan|--output:deploy:apply|--output:projects:list|--output:project:list|--output:projects:versions|--output:project:versions|--output:projects:info|--output:project:info|--output:gwemu:ps)
      candidates="text json" ;;
    --output:profile:list|--output:sdcard:list) candidates="text json names" ;;
    --output:profile:duplicate) candidates="text json" ;;
    --region:deploy:plan|--region:deploy:apply) candidates="bank1 bank2 frogfs littlefs extflash sd" ;;
    --programmer:deploy:apply|--programmer:debug:python|--programmer:profile:hardware|--programmer:perf:hardware)
      candidates="stlink jlink cmsis-dap rpi-gpio" ;;
    --target:debug:python) candidates="gwemu hardware" ;;
    --target:filesystem:*|--target:fs:*) candidates="flash/ext sdcard sd" ;;
    --filesystem:filesystem:create|--filesystem:fs:create) candidates="frogfs littlefs lfs fatfs sdcard sd" ;;
    --variant:projects:install|--variant:project:install|--variant:retro-go:install) candidates="flash sd" ;;
    --timing-mode:gwemu:start|--timing-mode:gwemu:run) candidates="default baseline experimental-m7" ;;
    --icount:gwemu:start|--icount:gwemu:run) candidates="0 1 2 3 4 5 6 7 8 9 10" ;;
    --bank:retro-go:build|--bank:gwemu:run) candidates="1 2" ;;
    --oc-level:retro-go:config) candidates="0 1 2 3" ;;
    --filesystem:media:inventory) candidates="frogfs littlefs fatfs" ;;
    --mode:media:compare) candidates="contents image" ;;
    --extflash-mib:profile:create) candidates="64 128 256" ;;
    --model:profile:create) candidates="auto mario zelda" ;;
    *) candidates="" ;;
  esac
  if [[ -n "$candidates" && ( "$prev" == "--variant" || "$prev" == "--format" || "$prev" == "--output" || "$prev" == "--region" || "$prev" == "--programmer" || "$prev" == "--target" || "$prev" == "--bank" || "$prev" == "--oc-level" || "$prev" == "--filesystem" || "$prev" == "--mode" || "$prev" == "--extflash-mib" || "$prev" == "--model" || "$prev" == "--timing-mode" || "$prev" == "--icount" ) ]]; then
    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
    return
  fi
  if [[ "$prev" == "--input" || "$prev" == "--input-dir" || "$prev" == "--firmware" || "$prev" == "--bios" || "$prev" == "--firmware-dir" || "$prev" == "--bios-dir" || "$prev" == "--game" || "$prev" == "--game-dir" || "$prev" == "--content" || "$prev" == "--source" || "$prev" == "--profile" || "$prev" == "--output" || "$prev" == "--output-dir" || "$prev" == "--bootloader-file" || "$prev" == "--backup-dir" || "$prev" == "--source-tree" || "$prev" == "--image" || "$prev" == "--shared-sd-root" || "$prev" == "--retro-go-root" || "$prev" == "--rom" || "$prev" == "--rom-dir" || "$prev" == "--config" || "$prev" == "--timeline" || "$prev" == "--record-timeline" || "$prev" == "--bank1" || "$prev" == "--bank2" || "$prev" == "--extflash" || "$prev" == "--sdcard" || "$prev" == "--symbols" || "$prev" == "--debug-config" || "$prev" == "--app-symbols" || "$prev" == "--gdb" || "$prev" == "--gwemu-bin" ]]; then
    _gwprov_complete_dirs "$cur"
    return
  fi
  if (( COMP_CWORD == 1 )) && [[ "$cur" != -* ]]; then
    candidates=$(gwprov tree --format names 2>/dev/null)
  elif (( COMP_CWORD == 2 )) && [[ "$cur" != -* ]]; then
    case "${COMP_WORDS[1]}" in
      show) candidates="adapters devices projects profile perf sdcard" ;;
      config) candidates="show" ;;
      set) candidates="active profile sdcard" ;;
      deploy) candidates="plan apply" ;;
      ps) candidates="--help -h --output --no-pager --profile" ;;
      completion) candidates="bash zsh" ;;
      adapters) candidates="list add remove rm" ;;
      device) candidates="recover" ;;
      projects|project) candidates="list versions info install stage-local" ;;
      tree) candidates="--format --no-pager" ;;
      debug) candidates="python" ;;
      retro-go) candidates="install build config" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sdcard|sd) candidates="list ls add remove rm create compose" ;;
      input) candidates="tap" ;;
      report) candidates="render" ;;
      profile) candidates="create duplicate list show" ;;
        perf) candidates="hardware" ;;
      filesystem|fs) candidates="create ls tree add delete del remove rm" ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 3 )) && [[ ( "${COMP_WORDS[1]-}" == sdcard || "${COMP_WORDS[1]-}" == sd ) && "${COMP_WORDS[2]-}" == add && "$cur" != -* ]]; then
    _gwprov_complete_dirs "$cur" directories
    return
  elif (( COMP_CWORD == 3 )) && [[ ( "${COMP_WORDS[1]-}" == sdcard || "${COMP_WORDS[1]-}" == sd ) && ( "${COMP_WORDS[2]-}" == create || "${COMP_WORDS[2]-}" == compose ) && "$cur" != -* ]]; then
    _gwprov_complete_dirs "$cur"
    return
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == projects || "${COMP_WORDS[1]-}" == project ]] && [[ "$cur" != -* ]]; then
    case "${COMP_WORDS[2]-}" in
      list|versions|info|install) candidates=$(_gwprov_project_names) ;;
      stage-local)
        COMPREPLY=()
        while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(compgen -f -- "$cur")
        return
        ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == config && "$cur" != -* ]]; then
    candidates="show"
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == set && "${COMP_WORDS[2]-}" == active && "$cur" != -* ]]; then
    candidates=$(_gwprov_device_names)
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == set && "${COMP_WORDS[2]-}" == profile && "$cur" != -* ]]; then
    candidates="$(_gwprov_profile_names) clear none"
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == set && "${COMP_WORDS[2]-}" == sdcard && "$cur" != -* ]]; then
    candidates="$(_gwprov_sdcard_names) clear none"
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == profile && "${COMP_WORDS[2]-}" == show && "$cur" != -* ]]; then
    candidates=$(_gwprov_profile_names)
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == profile && "${COMP_WORDS[2]-}" == duplicate && "$cur" != -* ]]; then
    candidates=$(_gwprov_profile_names)
  elif (( COMP_CWORD == 3 )) && [[ ( "${COMP_WORDS[1]-}" == sdcard || "${COMP_WORDS[1]-}" == sd ) && ( "${COMP_WORDS[2]-}" == remove || "${COMP_WORDS[2]-}" == rm ) && "$cur" != -* ]]; then
    candidates=$(_gwprov_sdcard_names)
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == ofw && "${COMP_WORDS[2]-}" == patch && "$cur" != -* ]]; then
    candidates="mario zelda"
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == filesystem || "${COMP_WORDS[1]-}" == fs ]] && [[ "${COMP_WORDS[2]-}" == create && "$cur" != -* ]]; then
    candidates="frogfs littlefs lfs sdcard sd"
  else
    extra_candidates="--help -h"
    case "$context" in
      ps:*) candidates="--output --no-pager --profile" ;;
      config:show) candidates="--output" ;;
      deploy:plan) candidates="--profile --region --output" ;;
      deploy:apply) candidates="--profile --probe-id --programmer --remote-url --remote-origin --region --output" ;;
      projects:list|project:list) candidates="--firmware-repo --output" ;;
      projects:versions|project:versions) candidates="--output" ;;
      projects:info|project:info) candidates="--version --output" ;;
      projects:install|project:install) candidates="--version --target --variant --output --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run" ;;
      projects:stage-local|project:stage-local) candidates="--output" ;;
      adapters:list) candidates="--output" ;;
      adapters:add) candidates="--origin" ;;
      tree:*) candidates="--format --no-pager" ;;
      retro-go:install) candidates="--repo --version --variant --output" ;;
      retro-go:build) candidates="--path --make-target --bank --extflash-part-mb --extflash-offset-mb --jobs --makevar --clean --docker --quiet --dry-run" ;;
      retro-go:config) candidates="--output --rom --menu-timeout --oc-level --selected-tab --cursor --browse-subpath" ;;
      gwemu:start) candidates="--profile --gdb-port --audio --headless --timeline --record-timeline --timing-mode --icount --rtc-epoch --gwemu-bin" ;;
      gwemu:stop) candidates="--profile --pid --timeout" ;;
      gwemu:pause|gwemu:resume) candidates="--profile --pid" ;;
      gwemu:ps) candidates="--output --no-pager" ;;
      gwemu:screenshot) candidates="--profile --pid --output" ;;
      gwemu:profile) candidates="--profile --symbols --duration --interval --progress-symbol --progress-interval --stall-threshold --rebase --debug-config --output --format --top" ;;
      gwemu:diagnose) candidates="--profile --symbols --output --max-frames --u32 --deref --bytes --value --ring --debug-config" ;;
      gwemu:watch) candidates="--profile --symbols --progress-symbol --guest-pc-symbol --rebase --u32 --deref --bytes --value --ring --interval --stall-after --duration --output --debug-config" ;;
      gwemu:run) candidates="--profile --gdb-port --shared-sd-root --bank1 --bank2 --extflash --sdcard --bank --timeline --record-timeline --icount --timing-mode --rtc-epoch --gwemu-bin --headless --audio --keep-temp --stdio-gdb" ;;
      gwemu:debug) candidates="--profile --gdb-port --headless --symbols --gdb --audio --no-break-on-fault --unpause-homebrew --app-symbols --detach-after-app-entry --keep-running --timeline --record-timeline" ;;
      debug:python) candidates="--target --host --port --openocd-port --probe-id --programmer --remote-url --remote-origin --qmp-socket --profile --symbols --debug-config" ;;
      ofw:patch) candidates="--source-tree --backup-dir --output-dir" ;;
      media:inventory) candidates="--profile --image --filesystem --offset --size --block-size --shared-sd-root --output" ;;
      filesystem:create|fs:create) candidates="--profile --image --target --filesystem --offset --size --size-mib --block-size --force" ;;
      filesystem:ls|filesystem:tree|filesystem:add|filesystem:delete|filesystem:del|filesystem:remove|filesystem:rm|fs:ls|fs:tree|fs:add|fs:delete|fs:del|fs:remove|fs:rm) candidates="--profile --image --target --filesystem --offset --size --block-size --source" ;;
      input:tap) candidates="--probe-id --programmer --remote-url --remote-origin --repeat --tap-ms --gap-ms" ;;
      media:compare) candidates="--mode" ;;
      media:frogfs|media:littlefs) candidates="--retro-go-root" ;;
      sdcard:create|sd:create) candidates="--size-mb --label" ;;
      sdcard:compose|sd:compose) candidates="--content-dir --core --core-name --rom --rom-dir --config" ;;
      input:tap) candidates="--repeat --tap-ms --gap-ms" ;;
      report:render) candidates="--input --output --format" ;;
      profile:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      perf:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      profile:create) candidates="--stock --content --backup-dir --locked --model --output-dir --name --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file" ;;
      profile:duplicate) candidates="--output-dir --output" ;;
      profile:list) candidates="--output --no-pager" ;;
      profile:show) candidates="--shared-sd-root" ;;
      completion) candidates="bash zsh" ;;
      *) candidates="" ;;
    esac
  fi
  COMPREPLY=( $(compgen -W "${candidates} ${extra_candidates}" -- "${cur}") )
}
complete -F _gwprov_complete gwprov""")
    return 0


def _completion_zsh(args) -> int:
    print(r'''#compdef gwprov
_gwprov_project_names() {
  local catalog
  catalog=$(gwprov projects list --output json 2>/dev/null) || return
  print -r -- "${(f)$(python3 -c 'import json,sys,urllib.parse; projects=json.load(sys.stdin)["projects"]; [(print(p["project"]), print((urllib.parse.urlparse(p["versionsUrl"]).hostname or "").split(".",1)[0]+"/"+p["project"])) for p in projects]' <<< "$catalog")}"
}
_gwprov_profile_names() { gwprov profile list --output names 2>/dev/null }
_gwprov_device_names() { gwprov devices --ids-only 2>/dev/null }
_gwprov_sdcard_names() { gwprov sdcard list --output names 2>/dev/null }
_gwprov_profile_complete() {
  if [[ "$1" == */* || "$1" == ~* ]]; then
    _files -/
  else
    compadd -- ${(f)"$(_gwprov_profile_names)"}
  fi
}
_gwprov() {
  local cur prev context candidates prefix pathpart
  local -a words_to_add
  cur=${words[CURRENT]}
  prev=${words[CURRENT-1]}
  context="${words[2]}:${words[3]}"

  case "$cur" in
    --profile=*)
      prefix=${cur%%=*}=; pathpart=${cur#*=}
      IPREFIX=$prefix PREFIX=$pathpart _gwprov_profile_complete "$pathpart"
      return
      ;;
    --input=*|--input-dir=*|--firmware=*|--bios=*|--firmware-dir=*|--bios-dir=*|--game=*|--game-dir=*|--content=*|--source=*|--profile=*|--output-dir=*|--bootloader-file=*|--backup-dir=*|--source-tree=*|--image=*|--shared-sd-root=*|--retro-go-root=*|--rom=*|--rom-dir=*|--config=*|--timeline=*|--record-timeline=*|--bank1=*|--bank2=*|--extflash=*|--sdcard=*|--symbols=*|--debug-config=*|--app-symbols=*|--gdb=*|--gwemu-bin=*)
      prefix=${cur%%=*}=; pathpart=${cur#*=}
      IPREFIX=$prefix PREFIX=$pathpart _files
      return
      ;;
    --variant=*) IPREFIX=--variant= PREFIX=${cur#*=}; compadd -- flash sd; return ;;
    --format=*)
      case "$context" in
        tree:*) compadd -- "${cur%%=*}=tree" "${cur%%=*}=names" ;;
        gwemu:profile) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" ;;
        profile:hardware|perf:hardware) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" "${cur%%=*}=html" "${cur%%=*}=pdf" ;;
        report:render) compadd -- "${cur%%=*}=html" "${cur%%=*}=pdf" ;;
      esac
      return
      ;;
    --output=*)
      case "$context" in
        profile:list|sdcard:list) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" "${cur%%=*}=names"; return ;;
        profile:duplicate) compadd -- "${cur%%=*}=text" "${cur%%=*}=json"; return ;;
        ps:*|apply:|deploy:plan|deploy:apply|projects:list|project:list|projects:versions|project:versions|projects:info|project:info|devices|adapters:list|gwemu:ps)
          compadd -- "${cur%%=*}=text" "${cur%%=*}=json"; return ;;
      esac
      prefix=${cur%%=*}=; pathpart=${cur#*=}; IPREFIX=$prefix PREFIX=$pathpart _files; return
      ;;
  esac
  case "$prev" in
    --profile) _gwprov_profile_complete "$cur"; return ;;
    --input|--input-dir|--firmware|--bios|--firmware-dir|--bios-dir|--game|--game-dir|--content|--source|--profile|--output|--output-dir|--bootloader-file|--backup-dir|--source-tree|--image|--shared-sd-root|--retro-go-root|--rom|--rom-dir|--config|--timeline|--record-timeline|--bank1|--bank2|--extflash|--sdcard|--symbols|--debug-config|--app-symbols|--gdb|--gwemu-bin)
      _files; return ;;
    --variant) compadd -- flash sd; return ;;
  esac
  case "$prev:$context" in
    --format:tree:*) compadd -- tree names; return ;;
    --format:gwemu:profile) compadd -- text json; return ;;
    --format:profile:hardware|--format:perf:hardware) compadd -- text json html pdf; return ;;
    --format:report:render) compadd -- html pdf; return ;;
    --output:profile:list|--output:sdcard:list) compadd -- text json names; return ;;
    --output:profile:duplicate) compadd -- text json; return ;;
    --output:ps:*|--output:apply:|--output:deploy:plan|--output:deploy:apply|--output:projects:list|--output:project:list|--output:projects:versions|--output:project:versions|--output:projects:info|--output:project:info|--output:gwemu:ps) compadd -- text json; return ;;
    --region:deploy:plan|--region:deploy:apply) compadd -- bank1 bank2 frogfs littlefs extflash sd; return ;;
    --programmer:deploy:apply|--programmer:debug:python|--programmer:profile:hardware|--programmer:perf:hardware) compadd -- stlink jlink cmsis-dap rpi-gpio; return ;;
    --target:debug:python) compadd -- gwemu hardware; return ;;
    --target:filesystem:*|--target:fs:*) compadd -- flash/ext sdcard sd; return ;;
    --filesystem:filesystem:create|--filesystem:fs:create) compadd -- frogfs littlefs lfs fatfs sdcard sd; return ;;
    --icount:gwemu:start|--icount:gwemu:run) compadd -- 0 1 2 3 4 5 6 7 8 9 10; return ;;
    --timing-mode:gwemu:start|--timing-mode:gwemu:run) compadd -- default baseline experimental-m7; return ;;
    --bank:retro-go:build|--bank:gwemu:run) compadd -- 1 2; return ;;
    --oc-level:retro-go:config) compadd -- 0 1 2 3; return ;;
    --filesystem:media:inventory) compadd -- frogfs littlefs fatfs; return ;;
    --mode:media:compare) compadd -- contents image; return ;;
    --extflash-mib:profile:create) compadd -- 64 128 256; return ;;
    --model:profile:create) compadd -- auto mario zelda; return ;;
  esac

  if (( CURRENT == 2 )) && [[ "$cur" != -* ]]; then
    candidates="$(gwprov tree --format names 2>/dev/null)"
  elif (( CURRENT == 3 )) && [[ "$cur" != -* ]]; then
    case ${words[2]} in
      deploy) candidates="plan apply" ;;
      ps) candidates="--help -h --output --no-pager --profile" ;;
      completion) candidates="bash zsh" ;;
      adapters) candidates="list add remove rm" ;;
      device) candidates="recover" ;;
      projects|project) candidates="list versions info install stage-local" ;;
      tree) candidates="--format --no-pager" ;;
      debug) candidates="python" ;;
      retro-go) candidates="install build config" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sdcard|sd) candidates="list ls add remove rm create compose" ;;
      input) candidates="tap" ;;
      report) candidates="render" ;;
      profile) candidates="create duplicate list show" ;;
      perf) candidates="hardware" ;;
      filesystem|fs) candidates="create ls tree add delete del remove rm" ;;
      show) candidates="adapters devices projects profile perf sdcard" ;;
      config) candidates="show" ;;
      set) candidates="active profile sdcard" ;;
      *) candidates="" ;;
    esac
  elif (( CURRENT == 4 )) && [[ ( ${words[2]} == sdcard || ${words[2]} == sd ) && ${words[3]} == add && "$cur" != -* ]]; then
    _files -/
    return
  elif (( CURRENT == 4 )) && [[ ( ${words[2]} == sdcard || ${words[2]} == sd ) && ( ${words[3]} == create || ${words[3]} == compose ) && "$cur" != -* ]]; then
    _files
    return
  elif (( CURRENT == 4 )) && [[ ${words[2]} == projects || ${words[2]} == project ]] && [[ "$cur" != -* ]]; then
    case ${words[3]} in
      list|versions|info|install) candidates="$( _gwprov_project_names )" ;;
      stage-local) _files; return ;;
      *) candidates="" ;;
    esac
  elif (( CURRENT == 4 )) && [[ ${words[2]} == config && "$cur" != -* ]]; then
    candidates="show"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == set && ${words[3]} == active && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_device_names)}"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == set && ${words[3]} == profile && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_profile_names)} clear none"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == set && ${words[3]} == sdcard && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_sdcard_names)} clear none"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == profile && ${words[3]} == show && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_profile_names)}"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == profile && ${words[3]} == duplicate && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_profile_names)}"
  elif (( CURRENT == 4 )) && [[ ( ${words[2]} == sdcard || ${words[2]} == sd ) && ( ${words[3]} == remove || ${words[3]} == rm ) && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_sdcard_names)}"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == ofw && ${words[3]} == patch && "$cur" != -* ]]; then
    candidates="mario zelda"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == filesystem || ${words[2]} == fs ]] && [[ ${words[3]} == create && "$cur" != -* ]]; then
    candidates="frogfs littlefs lfs sdcard sd"
  else
    case "$context" in
      ps:*) candidates="--output --no-pager --profile" ;;
      config:show) candidates="--output" ;;
      deploy:plan) candidates="--profile --region --output" ;;
      deploy:apply) candidates="--profile --probe-id --programmer --remote-url --remote-origin --region --output" ;;
      projects:list|project:list) candidates="--firmware-repo --output" ;;
      projects:versions|project:versions) candidates="--output" ;;
      projects:info|project:info) candidates="--version --output" ;;
      projects:install|project:install) candidates="--version --target --variant --output --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run" ;;
      projects:stage-local|project:stage-local) candidates="--output" ;;
      adapters:list) candidates="--output" ;;
      adapters:add) candidates="--origin" ;;
      tree:*) candidates="--format --no-pager" ;;
      retro-go:install) candidates="--repo --version --variant --output" ;;
      retro-go:build) candidates="--path --make-target --bank --extflash-part-mb --extflash-offset-mb --jobs --makevar --clean --docker --quiet --dry-run" ;;
      retro-go:config) candidates="--output --rom --menu-timeout --oc-level --selected-tab --cursor --browse-subpath" ;;
      gwemu:start) candidates="--profile --gdb-port --audio --headless --timeline --record-timeline --timing-mode --icount --rtc-epoch --gwemu-bin" ;;
      gwemu:stop) candidates="--profile --pid --timeout" ;;
      gwemu:pause|gwemu:resume) candidates="--profile --pid" ;;
      gwemu:ps) candidates="--output --no-pager" ;;
      gwemu:screenshot) candidates="--profile --pid --output" ;;
      gwemu:profile) candidates="--profile --symbols --duration --interval --progress-symbol --progress-interval --stall-threshold --rebase --debug-config --output --format --top" ;;
      gwemu:diagnose) candidates="--profile --symbols --output --max-frames --u32 --deref --bytes --value --ring --debug-config" ;;
      gwemu:watch) candidates="--profile --symbols --progress-symbol --guest-pc-symbol --rebase --u32 --deref --bytes --value --ring --interval --stall-after --duration --output --debug-config" ;;
      gwemu:run) candidates="--profile --gdb-port --shared-sd-root --bank1 --bank2 --extflash --sdcard --bank --timeline --record-timeline --icount --timing-mode --rtc-epoch --gwemu-bin --headless --audio --keep-temp --stdio-gdb" ;;
      gwemu:debug) candidates="--profile --gdb-port --headless --symbols --gdb --audio --no-break-on-fault --unpause-homebrew --app-symbols --detach-after-app-entry --keep-running --timeline --record-timeline" ;;
      debug:python) candidates="--target --host --port --openocd-port --probe-id --programmer --remote-url --remote-origin --qmp-socket --profile --symbols --debug-config" ;;
      ofw:patch) candidates="--source-tree --backup-dir --output-dir" ;;
      media:inventory) candidates="--profile --image --filesystem --offset --size --block-size --shared-sd-root --output" ;;
      filesystem:create|fs:create) candidates="--profile --image --target --filesystem --offset --size --size-mib --block-size --force" ;;
      filesystem:ls|filesystem:tree|filesystem:add|filesystem:delete|filesystem:del|filesystem:remove|filesystem:rm|fs:ls|fs:tree|fs:add|fs:delete|fs:del|fs:remove|fs:rm) candidates="--profile --image --target --filesystem --offset --size --block-size --source" ;;
      input:tap) candidates="--probe-id --programmer --remote-url --remote-origin --repeat --tap-ms --gap-ms" ;;
      media:compare) candidates="--mode" ;;
      media:frogfs|media:littlefs) candidates="--retro-go-root" ;;
      sdcard:create|sd:create) candidates="--size-mb --label" ;;
      sdcard:compose|sd:compose) candidates="--content-dir --core --core-name --rom --rom-dir --config" ;;
      input:tap) candidates="--repeat --tap-ms --gap-ms" ;;
      report:render) candidates="--input --output --format" ;;
      profile:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      perf:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      profile:create) candidates="--stock --content --backup-dir --locked --model --output-dir --name --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file" ;;
      profile:duplicate) candidates="--output-dir --output" ;;
      profile:list) candidates="--output --no-pager" ;;
      profile:show) candidates="--shared-sd-root" ;;
      *) candidates="" ;;
    esac
    candidates="$candidates --help -h"
  fi
  words_to_add=( ${(z)candidates} )
  compadd -- $words_to_add
}
compdef _gwprov gwprov''')
    return 0


def _command_tree(args) -> int:
    parser = build_parser()
    actions = [action for action in parser._actions
               if isinstance(action, argparse._SubParsersAction)]
    if args.format == "names":
        choices = actions[0].choices if actions else {}
        ordered = [name for _, group in getattr(parser, "help_categories", ())
                   for name in group if name in choices]
        ordered_set = set(ordered)
        ordered.extend(name for name in choices if name not in ordered_set)
        for name in ordered:
            print(name)
        return 0

    root = Tree(Text("gwprov", style="bold bright_cyan"), guide_style="bright_black")

    def add_commands(current: argparse.ArgumentParser, parent: Tree,
                     selected: list[str] | None = None) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            names = selected if selected is not None else list(action.choices)
            added_parsers = set()
            for name in names:
                child = action.choices.get(name)
                if child is None or getattr(child, "_gwprov_hidden", False):
                    continue
                if id(child) in added_parsers:
                    continue
                added_parsers.add(id(child))
                help_text = descriptions.get(name) or child.description or ""
                label = Text(name, style="bold cyan")
                if help_text:
                    label.append("  ")
                    label.append(help_text, style="dim")
                branch = parent.add(label)
                add_commands(child, branch)

    top_level = actions[0].choices if actions else {}
    categories = getattr(parser, "help_categories", ())
    shown: set[str] = set()
    shown_parsers: set[int] = set()
    for title, names in categories:
        members = [name for name in names if name in top_level]
        if not members:
            continue
        shown.update(members)
        shown_parsers.update(id(top_level[name]) for name in members)
        category = root.add(Text(title, style="bold bright_white"))
        add_commands(parser, category, members)
    remaining = [name for name in top_level if name not in shown and id(top_level[name]) not in shown_parsers]
    if remaining:
        category = root.add(Text("Other commands", style="bold bright_white"))
        add_commands(parser, category, remaining)

    guide = Group(
        root,
        Text("Use `gwprov COMMAND --help` for options and examples.  "
             "Use `gwprov tree --format names` for script-friendly command names.",
             style="dim"),
    )
    console = Console()
    line_count = len(console.render_lines(guide, console.options))
    should_page = console.is_terminal and not args.no_pager and line_count > console.height
    if should_page:
        with console.pager(styles=True):
            console.print(guide)
    else:
        console.print(guide)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = GWProvArgumentParser(
        prog="gwprov",
        description="Provision, inspect, and debug Game & Watch Retro-Go systems and GWemu.",
        epilog=("Common starting points:\n\n"
                "  Overview              gwprov show\n"
                "  Select a device       gwprov set active DEVICE_ID\n"
                "  Assign a profile      gwprov set profile PROFILE\n"
                "  Apply to the device   gwprov apply\n"
                "  Detailed device state gwprov ps\n"
                "  Add remote adapter    gwprov adapters add NAME ws://HOST:PORT/gdb\n"
                "  Browse projects       gwprov projects list\n"
                "  Browse all commands   gwprov tree\n\n"
                "Run `gwprov COMMAND --help` for command options and examples."),
    )
    parser.help_categories = (
        ("Overview and selection", ("show", "set", "ps")),
        ("All devices and adapters", ("devices", "adapters")),
        ("Selected-device controls", ("apply", "device", "gwemu", "input", "deploy", "perf", "config")),
        ("Profiles and storage", ("profile", "filesystem", "media", "sdcard")),
        ("Project catalog", ("projects",)),
        ("File workflows not yet device-scoped", ("retro-go", "ofw")),
        ("Debugging and reports", ("debug", "report")),
        ("Help and shell integration", ("tree", "completion")),
    )
    commands = parser.add_subparsers(dest="command", required=True,
                                     metavar="COMMAND",
                                     parser_class=GWProvArgumentParser)
    show = commands.add_parser("show", help="overview of GWProv resources")
    show.add_argument("section", nargs="?", choices=("adapters", "devices", "projects", "profile", "perf", "sdcard"))
    show.add_argument("--output", choices=("text", "json"), default="text")
    show.add_argument("--firmware-repo", default="sylverb/game-and-watch-retro-go-sd",
                      help=argparse.SUPPRESS)
    show.set_defaults(handler=_show)
    adapters = commands.add_parser("adapters", help="list and manage hardware adapters")
    adapters.set_defaults(handler=_adapters_list, output="text")
    adapter_commands = adapters.add_subparsers(dest="adapter_command")
    adapter_list = adapter_commands.add_parser("list", help="list local and registered adapters")
    adapter_list.add_argument("--output", choices=("text", "json"), default="text")
    adapter_list.set_defaults(handler=_adapters_list)
    adapter_add = adapter_commands.add_parser("add", help="register a remote gnwmanager adapter")
    adapter_add.add_argument("name", help="short local name for this remote adapter")
    adapter_add.add_argument("url", help="WebSocket endpoint, such as ws://host:8765/gdb")
    adapter_add.add_argument("--origin", help="Origin header required by the remote server")
    adapter_add.set_defaults(handler=_adapters_add)
    adapter_remove = adapter_commands.add_parser("remove", aliases=("rm",), help="remove a registered remote adapter")
    adapter_remove.add_argument("name_or_url", metavar="NAME_OR_URL")
    adapter_remove.set_defaults(handler=_adapters_remove)
    devices = commands.add_parser("devices", help="list GWemu and physical G&W devices")
    devices.add_argument("--output", choices=("text", "json"), default="text")
    devices.add_argument("--ids-only", action="store_true", help=argparse.SUPPRESS)
    devices.set_defaults(handler=_devices_command)
    device = commands.add_parser("device", help="inspect or recover the active physical device")
    device_commands = device.add_subparsers(dest="device_command", required=True)
    device_recover = device_commands.add_parser(
        "recover", help="recover a quiescent gnwmanager RAM service and boot bank 1")
    device_recover.set_defaults(handler=_device_recover)
    device_config = commands.add_parser("config", help="inspect configuration stored on the active device")
    config_commands = device_config.add_subparsers(dest="device_config_command", required=True)
    config_show = config_commands.add_parser("show", help="read and decode Retro-Go's current /CONFIG")
    config_show.add_argument("--output", choices=("text", "json"), default="text")
    config_show.set_defaults(handler=_device_config)
    set_command = commands.add_parser("set", help="choose the device focused by device commands")
    set_commands = set_command.add_subparsers(dest="set_command", required=True)
    active = set_commands.add_parser("active", help="show or select the active device")
    active.add_argument("device", nargs="?", help="device ID shown by `gwprov show devices`")
    active.add_argument("--remote-origin", help="Origin required by a selected WebSocket server")
    active.set_defaults(handler=_set_active)
    for field, help_text in (("profile", "assign a device profile to the active device"),
                             ("sdcard", "assign a registered SD card to the active device")):
        assignment = set_commands.add_parser(field, help=help_text)
        assignment.add_argument("value", nargs="?", metavar="NAME")
        assignment.set_defaults(handler=_set_assignment)
    sdcard = commands.add_parser("sdcard", aliases=("sd",),
                                 help="manage mounted SD cards and card images")
    sdcard_commands = sdcard.add_subparsers(dest="sdcard_command", required=True)
    sdcard_list = sdcard_commands.add_parser("list", aliases=("ls",), help="list registered SD cards")
    sdcard_list.add_argument("--output", choices=("text", "json", "names"), default="text",
                             help="table, JSON, or plain names for shell completion")
    sdcard_list.set_defaults(handler=_sdcard)
    sdcard_add = sdcard_commands.add_parser("add", help="register an already-mounted SD card folder or drive")
    sdcard_add.add_argument("path", help="mounted folder or drive path")
    sdcard_add.add_argument("name", nargs="?", help="card name; defaults to folder name or drive letter")
    sdcard_add.set_defaults(handler=_sdcard)
    sdcard_remove = sdcard_commands.add_parser("remove", aliases=("rm",), help="unregister an SD card")
    sdcard_remove.add_argument("name")
    sdcard_remove.set_defaults(handler=_sdcard)
    sdcard_create = sdcard_commands.add_parser("create", help="create a partitioned FAT32 SD image")
    sdcard_create.add_argument("image")
    sdcard_create.add_argument("--size-mb", type=int, required=True)
    sdcard_create.add_argument("--label", default="RETROGO")
    sdcard_create.set_defaults(handler=_sd_create)
    sdcard_compose = sdcard_commands.add_parser("compose", help="copy selected Retro-Go content onto an image")
    sdcard_compose.add_argument("image")
    sdcard_compose.add_argument("--content-dir")
    sdcard_compose.add_argument("--core")
    sdcard_compose.add_argument("--core-name", default="dos")
    sdcard_compose.add_argument("--rom", action="append", default=[])
    sdcard_compose.add_argument("--rom-dir", default="dos")
    sdcard_compose.add_argument("--config")
    sdcard_compose.set_defaults(handler=_sd_compose)
    apply = commands.add_parser("apply", help="apply the assigned profile to the active device")
    apply.add_argument("--output", choices=("text", "json"), default="text",
                       help="show live progress or emit a machine-readable result")
    apply.set_defaults(handler=_apply_parser)
    ps = commands.add_parser("ps", help="show live GWemu and hardware state")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    ps.add_argument("--profile", help="load firmware/app symbols for physical Application state")
    ps.set_defaults(handler=_ps)
    deploy = commands.add_parser("deploy", help="plan and apply firmware or filesystem writes")
    deploy_commands = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_plan = deploy_commands.add_parser("plan", help="show exact bank and filesystem writes")
    deploy_plan.add_argument("--profile", required=True)
    deploy_plan.add_argument("--region", action="append", choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"))
    deploy_plan.add_argument("--output", choices=("text", "json"), default="text")
    deploy_plan.set_defaults(handler=_deploy_plan)
    deploy_apply = deploy_commands.add_parser("apply", help="write selected profile regions to hardware")
    deploy_apply.add_argument("--profile", required=True)
    deploy_target = deploy_apply.add_mutually_exclusive_group()
    deploy_target.add_argument("--probe-id", help="select a local PyOCD probe by unique ID")
    deploy_target.add_argument("--programmer", choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                              help="select one local OpenOCD adapter explicitly")
    deploy_target.add_argument("--remote-url", help="use one gnwmanager serve URL")
    deploy_apply.add_argument("--remote-origin", help="Origin required by the selected remote server")
    deploy_apply.add_argument("--output", choices=("text", "json"), default="text",
                              help="show live progress or emit a machine-readable result")
    deploy_apply.add_argument("--region", action="append", choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"),
                              help="region to deploy; repeatable, defaults to all profile regions")
    deploy_apply.set_defaults(handler=_deploy_apply)
    tree = commands.add_parser("tree", help="browse every command and what it does")
    tree.add_argument("--format", choices=("tree", "names"), default="tree",
                      help="show the command map or plain names for scripts")
    tree.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    tree.set_defaults(handler=_command_tree)
    completion = commands.add_parser("completion", help="print a Bash or Zsh completion script")
    completion.add_argument("shell", choices=("bash", "zsh"))
    completion.set_defaults(handler=lambda args: _completion_bash(args) if args.shell == "bash" else _completion_zsh(args))

    debug_tools = commands.add_parser("debug", help="attach a Python debugger to hardware or GWemu")
    debug_commands = debug_tools.add_subparsers(dest="debug_command", required=True)
    python_debug = debug_commands.add_parser(
        "python", help="attach a persistent Python console to GWemu or hardware")
    python_debug.add_argument("--target", choices=("gwemu", "hardware"), required=True)
    python_debug.add_argument("--host", default="127.0.0.1")
    python_debug.add_argument("--port", type=int, default=1234,
                              help="GWemu GDB port (default: 1234)")
    python_debug.add_argument("--openocd-port", type=int, default=6666)
    python_debug.add_argument("--probe-id", action="append", default=[],
                              help="select a local PyOCD probe by unique ID (repeatable)")
    python_debug.add_argument("--programmer", action="append", default=[],
                              choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                              help="start an explicit OpenOCD adapter session (repeatable by type)")
    python_debug.add_argument("--remote-url", action="append", default=[],
                              help="connect to one gnwmanager serve URL; repeat for independent servers")
    python_debug.add_argument("--remote-origin", action="append", default=[],
                              help="Origin header for the corresponding remote URL (repeatable)")
    python_debug.add_argument("--qmp-socket", help="GWProv-managed QMP handle; normally discovered from --profile")
    python_debug.add_argument("--debug-config", help="local ELF/map, relocation and counter descriptor")
    python_debug.add_argument("--profile", help="load the profile's bundled official Retro-Go ELF symbols")
    python_debug.add_argument("--symbols", action="append", default=[], metavar="ELF",
                              help="load an additional firmware or app ELF (repeatable)")
    python_debug.set_defaults(handler=_debug_python)

    project = commands.add_parser("projects", aliases=("project",),
                                  help="inspect and stage GWRG-distributed projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list", help="list curated cores and homebrew projects")
    project_list.add_argument("name", nargs="?", help="optional project name or owner/name filter")
    project_list.add_argument("--firmware-repo", default="sylverb/game-and-watch-retro-go-sd",
                              help="firmware repository that publishes projects.json")
    project_list.add_argument("--output", choices=("text", "json"), default="text")
    project_list.set_defaults(handler=_project_list)
    versions = project_commands.add_parser("versions", help="list release versions")
    versions.add_argument("repo", help="curated project name or arbitrary owner/repo")
    versions.add_argument("--output", choices=("text", "json"), default="text")
    versions.set_defaults(handler=_project_versions)
    info = project_commands.add_parser("info", help="show a project's targets and required files")
    info.add_argument("repo", help="curated project name or arbitrary owner/repo")
    info.add_argument("--version", help="published tag; defaults to newest")
    info.add_argument("--output", choices=("text", "json"), default="text")
    info.set_defaults(handler=_project_info)
    install = project_commands.add_parser("install", help="verify and stage a project release")
    install.add_argument("repo", help="curated project name or arbitrary owner/repo")
    install.add_argument("--version", help="published tag; defaults to newest")
    install.add_argument("--target", help="target id when the manifest publishes more than one")
    install.add_argument("--variant", choices=("flash", "sd"), required=True,
                         help="provisioning variant to stage")
    install.add_argument("--output", required=True, help="staging root directory")
    install.add_argument("--input", action="append", default=[], metavar="SLOT=FILE",
                         help="converter input file; repeat for multiple files")
    install.add_argument("--input-dir", action="append", default=[], metavar="SLOT=DIR|DIR",
                         help="non-recursive converter input directory; use DIR for one-slot converters or SLOT=DIR otherwise")
    install.add_argument("--firmware", "--bios", dest="firmware", action="append", default=[],
                         metavar="ID=FILE", help="supply a firmware file (BIOS is an accepted synonym)")
    install.add_argument("--firmware-dir", "--bios-dir", dest="firmware_dir", action="append", default=[],
                         metavar="DIR", help="find declared firmware files in a directory")
    install.add_argument("--game", action="append", default=[], metavar="SYSTEM=FILE",
                         help="stage a game file and evaluate requiredFor firmware")
    install.add_argument("--game-dir", action="append", default=[], metavar="SYSTEM=DIR",
                         help="stage files from a system game directory")
    install.add_argument("--dry-run", action="store_true", help="resolve, verify and plan without writing")
    install.set_defaults(handler=_project_install)
    stage_local = project_commands.add_parser("stage-local", help="stage an unpublished local build into content")
    stage_local.add_argument("manifest", help="local JSON manifest describing sources and destinations")
    stage_local.add_argument("--output", required=True, help="content root populated by retro-go install")
    stage_local.set_defaults(handler=_project_stage_local)

    retro_go = commands.add_parser("retro-go", help="install, build, and configure Retro-Go")
    retro_commands = retro_go.add_subparsers(dest="retro_command", required=True)
    firmware_install = retro_commands.add_parser("install", help="download verified release firmware and content")
    firmware_install.add_argument("--repo", default="sylverb/game-and-watch-retro-go-sd")
    firmware_install.add_argument("--version")
    firmware_install.add_argument("--variant", choices=("flash", "sd"), required=True)
    firmware_install.add_argument("--output", required=True, help="content staging root shared with project install")
    firmware_install.set_defaults(handler=_firmware_install)
    build = retro_commands.add_parser("build", help="build using the existing Retro-Go wrapper")
    build.add_argument("--path", default="references/game-and-watch-retro-go-sd")
    build.add_argument("--make-target", default="gwemu_release")
    build.add_argument("--bank", type=int, choices=(1, 2), default=None)
    build.add_argument("--extflash-part-mb", type=int, default=None)
    build.add_argument("--extflash-offset-mb", type=int, default=None)
    build.add_argument("--jobs", type=int, default=None)
    build.add_argument("--makevar", action="append", default=[], metavar="KEY=VALUE")
    build.add_argument("--clean", action="store_true")
    build.add_argument("--docker", action="store_true")
    build.add_argument("--quiet", action="store_true")
    build.add_argument("--dry-run", action="store_true")
    build.set_defaults(handler=_build_retro_go)

    config = retro_commands.add_parser("config", help="write Retro-Go's /CONFIG file")
    config.add_argument("--output", required=True)
    config.add_argument("--rom", nargs=2, metavar=("CORE", "FILENAME"),
                        help="auto-launch a ROM, for example --rom gb Tetris.gb")
    config.add_argument("--menu-timeout", type=int, default=0)
    config.add_argument("--oc-level", type=int, choices=range(4), default=0)
    config.add_argument("--selected-tab", type=int, default=0)
    config.add_argument("--cursor", type=int, default=0)
    config.add_argument("--browse-subpath", default="")
    config.set_defaults(handler=_make_config)

    gwemu = commands.add_parser("gwemu", help="launch and control GWemu targets")
    gwemu_commands = gwemu.add_subparsers(dest="gwemu_command", required=True)
    start = gwemu_commands.add_parser("start", help="start one managed GWemu profile")
    start.add_argument("--profile", required=True)
    start.add_argument("--gdb-port", type=int, help="GDB port; default: choose an available port")
    start.add_argument("--audio", action="store_true")
    start.add_argument("--headless", action="store_true",
                       help="run without the window (visible by default)")
    start_inputs = start.add_mutually_exclusive_group()
    start_inputs.add_argument("--timeline", help="replay inputs on GWemu guest time")
    start_inputs.add_argument("--record-timeline", metavar="FILE.tl",
                              help="record GUI inputs on guest time; requires a new file")
    start.add_argument("--timing-mode", choices=("default", "baseline", "experimental-m7"), default="default",
                       help="baseline: coherent one-instruction/one-cycle DWT; experimental, not hardware accurate")
    start.add_argument("--icount", type=int, choices=range(11), help="fixed instruction-count shift; baseline requires 0")
    start.add_argument("--gwemu-bin", help="explicit GWemu executable; retain a fixed binary for timing cohorts")
    start.add_argument("--rtc-epoch", type=int, help="repeatable RTC seed as Unix seconds")
    start.set_defaults(handler=_gwemu_start)

    stop = gwemu_commands.add_parser("stop", help="gracefully stop a daemon-managed instance")
    stop_target = stop.add_mutually_exclusive_group(required=False)
    stop_target.add_argument("--profile", help="profile directory of the instance")
    stop_target.add_argument("--pid", type=int, help="GWemu process id")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.set_defaults(handler=_gwemu_stop)

    pause = gwemu_commands.add_parser("pause", help="pause a GWemu instance through the daemon")
    pause_target = pause.add_mutually_exclusive_group()
    pause_target.add_argument("--profile", help="override the active GWemu device")
    pause_target.add_argument("--pid", type=int, help="select a GWemu process explicitly")
    pause.set_defaults(handler=_gwemu_pause)

    resume = gwemu_commands.add_parser("resume", help="resume a paused GWemu instance through the daemon")
    resume_target = resume.add_mutually_exclusive_group()
    resume_target.add_argument("--profile", help="override the active GWemu device")
    resume_target.add_argument("--pid", type=int, help="select a GWemu process explicitly")
    resume.set_defaults(handler=_gwemu_resume)

    ps = gwemu_commands.add_parser("ps", help="list GWemu instances and execution state")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    ps.set_defaults(handler=_gwemu_ps)

    screenshot = gwemu_commands.add_parser(
        "screenshot", help="capture a GWemu PNG and report whether the frame is black")
    screenshot_target = screenshot.add_mutually_exclusive_group(required=False)
    screenshot_target.add_argument("--profile", help="profile directory of the instance")
    screenshot_target.add_argument("--pid", type=int, help="GWemu process id")
    screenshot.add_argument("--output", help="PNG path; defaults under the profile runtime directory")
    screenshot.set_defaults(handler=_gwemu_screenshot)

    profiler = gwemu_commands.add_parser(
        "profile", help="sample native hot functions through the GWProv daemon while the app runs")
    profiler.add_argument("--profile", help="override the active GWemu device")
    profiler.add_argument("--symbols", action="append", default=[], metavar="ELF",
                          help="additional symbols; bundled firmware/app ELFs load automatically")
    profiler.add_argument("--debug-config", help="local ELF/map, relocation and counter descriptor")
    profiler.add_argument("--duration", type=float, default=15.0)
    profiler.add_argument("--interval", type=float, default=0.02,
                          help="sample interval in wall seconds, with jitter (default: 0.02)")
    profiler.add_argument("--progress-symbol", action="append", metavar="SYMBOL",
                          help="scalar progress counter (1, 2, 4 or 8 bytes); default: discover common counter names")
    profiler.add_argument("--progress-interval", type=float, default=1.0,
                          help="seconds between progress-counter observations; observed halted epochs are excluded")
    profiler.add_argument("--stall-threshold", type=float, default=1.0,
                          help="minimum duration in seconds for reporting a flat progress counter (default: 1.0)")
    profiler.add_argument("--rebase", action="append", default=[], metavar="SECTION=POINTER_SYMBOL",
                          help="map a linked section to its current runtime address")
    profiler.add_argument("--output", help="JSON report path; defaults under profile runtime")
    profiler.add_argument("--format", choices=("text", "json"), default="text")
    profiler.add_argument("--top", type=int, default=20,
                          help="number of rows printed; JSON always keeps all functions/samples")
    profiler.set_defaults(handler=_gwemu_profile)

    diagnose = gwemu_commands.add_parser(
        "diagnose", help="capture a running profile's screen and symbol-resolved call stack")
    diagnose.add_argument("--profile", help="override the active GWemu device")
    diagnose.add_argument("--symbols", action="append", default=[], metavar="ELF",
                          help="additional app ELF symbols (repeatable)")
    diagnose.add_argument("--debug-config", help="shared local port debug descriptor")
    diagnose.add_argument("--output", help="PNG path; defaults under the profile runtime directory")
    diagnose.add_argument("--max-frames", type=int, default=32)
    diagnose.add_argument("--u32", action="append", default=[], metavar="SYMBOL",
                          help="read a symbol-addressed 32-bit target value (repeatable)")
    diagnose.add_argument("--deref", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE",
                          help="read bytes from a pointer-valued global, e.g. --deref g_ppu:16 (repeatable)")
    diagnose.add_argument("--bytes", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE",
                          help="read bytes directly from a symbol, e.g. --bytes g_cpu:56 (repeatable)")
    diagnose.add_argument("--value", action="append", default=[], metavar="SYMBOL",
                          help="decode a C global using its ELF DWARF type (repeatable)")
    diagnose.add_argument("--ring", action="append", default=[], metavar="ARRAY:HEAD",
                          help="decode a C trace array with its monotonic write counter (repeatable)")
    diagnose.set_defaults(handler=_gwemu_diagnose)

    watch = gwemu_commands.add_parser(
        "watch", help="watch app progress and capture a report when it stalls")
    watch.add_argument("--profile", help="override the active GWemu device")
    watch.add_argument("--symbols", action="append", default=[], metavar="ELF",
                       help="app or firmware ELF symbols (repeatable; firmware is loaded from profile)")
    watch.add_argument("--debug-config", help="shared local port debug descriptor")
    watch.add_argument("--progress-symbol", action="append", default=[], metavar="SYMBOL",
                       help="32-bit counter that should change during healthy execution (repeatable)")
    watch.add_argument("--guest-pc-symbol", action="append", default=[], metavar="SYMBOL",
                       help="32-bit guest instruction PC to monitor for stagnation (repeatable)")
    watch.add_argument("--rebase", action="append", default=[], metavar="SECTION=POINTER_SYMBOL",
                       help="rebase an ELF section from a target pointer-valued symbol (repeatable)")
    watch.add_argument("--u32", action="append", default=[], metavar="SYMBOL",
                       help="include an additional 32-bit symbol in every sample and report")
    watch.add_argument("--deref", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE",
                       help="include bytes through a pointer-valued global in a trigger report")
    watch.add_argument("--bytes", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE",
                       help="include bytes directly from a global in a trigger report")
    watch.add_argument("--value", action="append", default=[], metavar="SYMBOL",
                          help="decode a C global using its ELF DWARF type (repeatable)")
    watch.add_argument("--ring", action="append", default=[], metavar="ARRAY:HEAD",
                          help="decode a C trace array with its monotonic write counter (repeatable)")
    watch.add_argument("--heartbeat-symbol", help=argparse.SUPPRESS)
    watch.add_argument("--frame-symbol", help=argparse.SUPPRESS)
    watch.add_argument("--interval", type=float, default=0.5)
    watch.add_argument("--stall-after", type=float, default=3.0)
    watch.add_argument("--duration", type=float, default=30.0)
    watch.add_argument("--output", help="JSON report path; defaults under profile runtime")
    watch.set_defaults(handler=_gwemu_watch)

    run = gwemu_commands.add_parser("run", help="launch a daemon-managed GWemu instance")
    run.add_argument("--profile")
    run.add_argument("--gdb-port", type=int, help="optional loopback debug server; guest starts running")
    run.add_argument("--shared-sd-root")
    run.add_argument("--bank1", default="")
    run.add_argument("--bank2", default="")
    run.add_argument("--extflash", default="")
    run.add_argument("--sdcard", default="")
    run.add_argument("--bank", type=int, choices=(1, 2), default=1)
    run_inputs = run.add_mutually_exclusive_group()
    run_inputs.add_argument("--timeline", help="replay inputs on GWemu guest time")
    run_inputs.add_argument("--record-timeline", metavar="FILE.tl",
                            help="record GUI inputs on guest time; requires a new file")
    run.add_argument("--icount", type=int, choices=range(11))
    run.add_argument("--timing-mode", choices=("default", "baseline", "experimental-m7"), default="default",
                     help="baseline: coherent one-instruction/one-cycle DWT; experimental, not hardware accurate")
    run.add_argument("--gwemu-bin", help="explicit GWemu executable; retain a fixed binary for timing cohorts")
    run.add_argument("--rtc-epoch", type=int, help="repeatable RTC seed as Unix seconds")
    run.add_argument("--headless", action="store_true")
    run.add_argument("--audio", action="store_true")
    run.add_argument("--keep-temp", action="store_true")
    run.add_argument("--stdio-gdb", action="store_true",
                     help="use the direct GDB-stdio harness (bypasses daemon QMP management)")
    run.set_defaults(handler=_run_gwemu)

    debug = gwemu_commands.add_parser("debug", help="run GWemu under GDB with bundled firmware symbols")
    debug.add_argument("--profile", help="override the active GWemu device")
    debug.add_argument("--gdb-port", type=int, default=1234)
    debug.add_argument("--headless", action="store_true",
                        help="run without a display window")
    debug.add_argument("--symbols", help="override profile's matching firmware ELF")
    debug.add_argument("--gdb", help="GDB executable (defaults to arm-none-eabi-gdb or gdb-multiarch)")
    debug.add_argument("--audio", action="store_true")
    debug.add_argument("--no-break-on-fault", action="store_true")
    debug.add_argument("--unpause-homebrew", action="store_true",
                       help="at reset, clear Retro-Go start_paused at run_gwhb_homebrew entry")
    debug.add_argument("--app-symbols", help="app ELF used to resolve app_main")
    debug.add_argument("--detach-after-app-entry", action="store_true",
                       help="after the launch hook, detach GDB at app_main so another gwprov monitor can attach")
    debug.add_argument("--keep-running", action="store_true",
                       help="leave GWemu running after GDB detaches or exits")
    debug_inputs = debug.add_mutually_exclusive_group()
    debug_inputs.add_argument("--timeline", help="replay inputs on GWemu guest time")
    debug_inputs.add_argument("--record-timeline", metavar="FILE.tl",
                              help="record GUI inputs on guest time; requires a new file")
    debug.set_defaults(handler=_debug_gwemu)

    ofw = commands.add_parser("ofw", help="prepare stock firmware images offline")
    ofw_commands = ofw.add_subparsers(dest="ofw_command", required=True)
    patch = ofw_commands.add_parser("patch", help="build patched stock banks using gnwmanager's offline patch pipeline")
    patch.add_argument("game", choices=("mario", "zelda"))
    patch.add_argument("--source-tree", required=True, help="qemu-gnw checkout")
    patch.add_argument("--backup-dir", required=True, help="directory of stock internal and external backups")
    patch.add_argument("--output-dir", required=True)
    patch.set_defaults(handler=_ofw_patch)

    media = commands.add_parser("media", help="inspect and build Retro-Go filesystems")
    media_commands = media.add_subparsers(dest="filesystem", required=True)
    inventory = media_commands.add_parser("inventory", help="read filesystem tables and hashes from a device profile")
    inventory_source = inventory.add_mutually_exclusive_group(required=True)
    inventory_source.add_argument("--profile")
    inventory_source.add_argument("--image")
    inventory.add_argument("--filesystem", choices=("frogfs", "littlefs", "fatfs"))
    inventory.add_argument("--offset", type=lambda value: int(value, 0), default=0)
    inventory.add_argument("--size", type=lambda value: int(value, 0))
    inventory.add_argument("--block-size", type=lambda value: int(value, 0), default=4096)
    inventory.add_argument("--shared-sd-root")
    inventory.add_argument("--output")
    inventory.set_defaults(handler=_media_inventory)
    comparison = media_commands.add_parser("compare", help="compare saved filesystem inventories or image hashes")
    comparison.add_argument("expected")
    comparison.add_argument("actual")
    comparison.add_argument("--mode", choices=("contents", "image"), default="contents")
    comparison.set_defaults(handler=_media_compare)
    for filesystem in ("frogfs", "littlefs"):
        pack = media_commands.add_parser(filesystem, help=f"run the vendored {filesystem} packer")
        pack.add_argument("--retro-go-root", required=True,
                          help="Retro-Go-SD checkout supplying tools, firmware constants, and sd_content")
        pack.set_defaults(handler=_fs_pack)

    filesystem = commands.add_parser(
        "filesystem", aliases=("fs",),
        help="inspect or edit profile/image filesystems",
        description=("FrogFS edits rebuild the packed image; LittleFS and SD edits happen in place. "
                     "Use `create TYPE N_MIB [TARGET_DIR] [FILENAME]` to create a filesystem image."))
    filesystem_commands = filesystem.add_subparsers(dest="fs_command", required=True)
    def filesystem_target(operation, *, required=True):
        target = operation.add_mutually_exclusive_group(required=required)
        target.add_argument("--profile", help="named profile or profile path")
        target.add_argument("--image", help="explicit filesystem image")
        operation.add_argument("--target", choices=("flash/ext", "sdcard", "sd"), default="flash/ext",
                               help="profile storage image (default: flash/ext)")
        operation.add_argument("--filesystem", choices=("frogfs", "littlefs", "fatfs", "sd"),
                               help="filesystem type for --image; on flash profiles, selects FrogFS or LittleFS")
        operation.add_argument("--offset", type=lambda value: int(value, 0), default=0)
        operation.add_argument("--size", type=lambda value: int(value, 0),
                               help="LittleFS region size or FrogFS creation capacity in bytes")
        operation.add_argument("--block-size", type=lambda value: int(value, 0), default=4096)
        operation.set_defaults(handler=_filesystem)
    create_fs = filesystem_commands.add_parser(
        "create", help="create or format a filesystem",
        usage="gwprov fs create TYPE N_MIB [TARGET_DIR] [FILENAME] [OPTIONS]",
        description=("Create a FrogFS, LittleFS, or partitioned SD image. N_MIB is the capacity in MiB. "
                     "TARGET_DIR defaults to the current directory; FILENAME defaults to frogfs.bin, "
                     "lfs.bin, or sdcard.bin for the selected type. Existing files require --force."))
    filesystem_target(create_fs, required=False)
    create_fs.add_argument("fs_type", nargs="?", choices=("frogfs", "littlefs", "lfs", "sdcard", "sd"),
                           help="filesystem type")
    create_fs.add_argument("size_mib_pos", nargs="?", type=int, metavar="N_MIB",
                           help="filesystem/image capacity in MiB")
    create_fs.add_argument("target_dir", nargs="?", help="output directory (default: current directory)")
    create_fs.add_argument("filename", nargs="?", help="output filename (defaults by filesystem type)")
    create_fs.add_argument("--size-mib", type=int, help="SD image capacity or profile SD size in MiB")
    create_fs.add_argument("--force", action="store_true", help="replace existing filesystem data")
    ls_fs = filesystem_commands.add_parser("ls", aliases=("tree",), help="list files and directories")
    filesystem_target(ls_fs)
    ls_fs.add_argument("path", nargs="?", help="directory inside the filesystem")
    add_fs = filesystem_commands.add_parser("add", help="add or replace a file")
    filesystem_target(add_fs)
    add_fs.add_argument("destination", help="destination path inside the filesystem")
    add_fs.add_argument("--source", required=True, help="local file to add")
    delete_fs = filesystem_commands.add_parser("delete", aliases=("del", "remove", "rm"), help="remove a file")
    filesystem_target(delete_fs)
    delete_fs.add_argument("path", help="file path inside the filesystem")

    input_group = commands.add_parser("input", help="send controller input to a target")
    input_commands = input_group.add_subparsers(dest="input_command", required=True)
    tap = input_commands.add_parser("tap", help="tap a button or chord over the probe")
    tap.add_argument("buttons", help="button name or chord, such as LEFT+GAME")
    tap.add_argument("--repeat", type=int, default=1)
    tap.add_argument("--tap-ms", type=int, default=80)
    tap.add_argument("--gap-ms", type=int, default=120)
    input_target = tap.add_mutually_exclusive_group()
    input_target.add_argument("--probe-id", help="select a local PyOCD probe")
    input_target.add_argument("--programmer", choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"))
    input_target.add_argument("--remote-url", help="gnwmanager websocket URL")
    tap.add_argument("--remote-origin", help="Origin required by the remote server")
    tap.set_defaults(handler=_input_tap)

    reports = commands.add_parser("report", help="render structured reports for review")
    report_commands = reports.add_subparsers(dest="report_command", required=True)
    render = report_commands.add_parser("render", help="render JSON as portable HTML or PDF")
    render.add_argument("--input", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--format", choices=("html", "pdf"), required=True)
    render.set_defaults(handler=_report_render)

    profile = commands.add_parser("profile", help="create and manage device profiles")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True,
                                              metavar="{create,duplicate,list,show}")
    create = profile_commands.add_parser("create", help="create a device profile from content or stock media")
    create.add_argument("directory", metavar="NAME_OR_PATH",
                        help="managed profile name, or an explicit output path")
    create.add_argument("--output-dir",
                        help="profile store for this creation (default: GWPROV_PROFILE_DIR or XDG data home)")
    source = create.add_mutually_exclusive_group()
    source.add_argument("--stock", action="store_true",
                        help="create pristine stock media from hash-valid backups")
    source.add_argument("--content", help="root populated by retro-go install and project install")
    create.add_argument("--backup-dir", action="append", default=[],
                        help="stock backup directory (repeatable; required with --stock)")
    create.add_argument("--locked", action="store_true",
                        help="initialize device RDP state as locked (requires --stock)")
    create.add_argument("--model", choices=("auto", "mario", "zelda"),
                        help="stock device model (requires --stock; default: auto)")
    create.add_argument("--name")
    create.add_argument("--littlefs-mib", type=int, default=2)
    create.add_argument("--sd-size-mib", type=int, default=128,
                        help="capacity of the bundled FAT32 image for SD firmware (default: 128)")
    create.add_argument("--sd-label", default="RETROGO",
                        help="volume label for the bundled SD image")
    create.add_argument("--extflash-mib", type=int, choices=(64, 128, 256),
                        help="chip capacity; flash defaults to the smallest size that fits, SD defaults to 64 MiB")
    create.add_argument("--bootloader-repo", default="sylverb/game-and-watch-bootloader")
    create.add_argument("--bootloader-version", default="v1.0.8", help="release tag or latest")
    create.add_argument("--bootloader-file", help="use a local binary linked at 0x08000000")
    create.set_defaults(handler=_profile_create)
    duplicate = profile_commands.add_parser(
        "duplicate", help="copy a profile into an independent working profile")
    duplicate.add_argument("source", metavar="SOURCE",
                           help="managed profile name or explicit profile path")
    duplicate.add_argument("destination", metavar="DESTINATION",
                           help="new managed profile name or explicit output path")
    duplicate.add_argument(
        "--output-dir",
        help="profile store for the copy (default: GWPROV_PROFILE_DIR or platform data directory)")
    duplicate.add_argument("--output", choices=("text", "json"), default="text")
    duplicate.set_defaults(handler=_profile_duplicate)
    profile_list = profile_commands.add_parser("list", help="list profiles in the managed profile store")
    profile_list.add_argument("--output", choices=("text", "json", "names"), default="text",
                              help="table, JSON, or plain names for shell completion")
    profile_list.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    profile_list.set_defaults(handler=_profile_list)
    # Keep the old spelling usable while placing this report under the distinct
    # performance-profile noun in the primary command tree.
    _add_hardware_perf_command(profile_commands, help_text=argparse.SUPPRESS, hidden=True)
    perf = commands.add_parser("perf", help="capture and inspect performance profiles")
    perf_commands = perf.add_subparsers(dest="perf_command", required=True)
    _add_hardware_perf_command(perf_commands,
                               help_text="profile routine cycles from the selected hardware device")
    show = profile_commands.add_parser("show", help="inspect a named or path-based device profile")
    show.add_argument("directory", metavar="NAME_OR_PATH")
    show.add_argument("--shared-sd-root")
    show.set_defaults(handler=_profile_show)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, forwarded = parser.parse_known_args(argv)
    args.forwarded = forwarded
    if forwarded and args.command not in {"media", "ofw"}:
        parser.error("unrecognized arguments: " + " ".join(forwarded))
    try:
        return args.handler(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"gwprov: {exc}", file=sys.stderr)
        return 2
