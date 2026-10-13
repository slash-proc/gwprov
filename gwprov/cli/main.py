"""Command-line entry point for common provisioning operations."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import tempfile
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
    result = install_firmware(args.output, variant=args.variant, repo=args.repo, version=args.version,
                              versions_url=getattr(args, "versions_url", None))
    print(f"Installed Retro-Go {result['version']} ({args.variant}) in {args.output}")
    return 0


def _backups(args) -> int:
    from gwprov import backups
    from rich.table import Table
    if args.backup_action == "search":
        rows = backups.search(args.directory)
    else:
        rows = backups.list_backups()
    if args.output == "json":
        print(json.dumps({"backups": rows}, indent=2))
        return 0
    console = Console()
    table = Table(title=f"Stock backups ({len(rows)})", expand=True)
    table.add_column("MODEL", no_wrap=True)
    table.add_column("BANK 1", overflow="fold")
    table.add_column("EXTFLASH", overflow="fold")
    table.add_column("LOCATION", overflow="fold")
    for row in rows:
        table.add_row(row["model"].title(), f'{row["bank1Bytes"]:,} bytes',
                      f'{row["bank0Bytes"]:,} bytes', row["directory"])
    console.print(table)
    return 0


def _retro_sources(args) -> int:
    from gwprov import retro_sources
    action = args.source_action
    if action in ("list", "ls"):
        result = retro_sources.list_sources()
        if args.output == "json":
            print(json.dumps(result, indent=2))
            return 0
        for row in result["sources"]:
            print(("* " if row["default"] else "  ") + row["id"])
        return 0
    if action == "add":
        result = retro_sources.add_source(args.source)
    elif action == "remove":
        result = retro_sources.remove_source(args.source)
    else:
        result = retro_sources.set_default(args.source)
    if getattr(args, "output", "text") == "json":
        print(json.dumps(result, indent=2))
    elif action == "default":
        print(f"Default Retro-Go source: {result['default']}")
    else:
        verb = "added" if action == "add" else "removed"
        print(f"Retro-Go source {verb}: {result['id']}")
    return 0


def _retro_release_list(args) -> int:
    from gwprov import retro_sources
    from rich.table import Table
    rows = retro_sources.list_releases(args.source)
    key = "version" if args.sort == "version" else "source"
    rows.sort(key=lambda row: str(row.get(key, "")).casefold(), reverse=args.reverse)
    if args.output == "json":
        print(json.dumps({"releases": rows}, indent=2))
        return 0
    table = Table(title=f"Retro-Go releases ({len(rows)})", expand=True)
    table.add_column("SOURCE", overflow="fold")
    table.add_column("VERSION", no_wrap=True)
    for row in rows:
        table.add_row(row["source"], row["version"])
    Console().print(table)
    return 0


def _project_stage_local(args) -> int:
    from gwprov.dist.local_project import stage_local_project
    result = stage_local_project(args.manifest, output=args.output)
    print(f"Staged local {result['repo']}:{result['target']} ({result['variant']})")
    for path in result['files']:
        print(f"  {path}")
    return 0


def _profile_create(args) -> int:
    from gwprov.profiles import profile_destination, profile_reference

    destination = profile_destination(args.directory, output_dir=args.output_dir)
    destination_label = profile_reference(destination, profile_dir=args.output_dir)
    model = "mario" if args.mario else "zelda" if args.zelda else None
    if model:
        from gwprov.backups import list_backups
        from gwprov.stock import create_stock_profile
        backups = list_backups(model)
        if not backups:
            raise ValueError(f"no discovered {model.title()} backup pair; run `gwprov backups search DIRECTORY`")
        result = create_stock_profile(destination,
                                      backup_dirs=[row["directory"] for row in backups],
                                      model=model, extflash_mib=args.extflash_mib or 64)
        if args.patched or args.bootloader:
            # Apply the stock transformation before adding Retro-Go's bank 2
            # image. Stock-only profiles start with an explicit erased bank 2
            # map entry, which would otherwise overlap the incoming firmware
            # and abort profile creation before the patch step was reached.
            from gwprov.vendor.qemu_gnw import cfw_images
            import hashlib, shutil, yaml
            out = destination / ".patch-output"
            backup_dir = Path(backups[0]["directory"])
            cfw_images.main([model, "--backup-dir", str(backup_dir),
                             "--output-dir", str(out), "--extflash-mib",
                             str(args.extflash_mib or 64),
                             *( [] if args.bootloader else ["--no-bootloader"] )])
            bank1 = (out / f"{model}-bank1-patched.bin").read_bytes()
            patched_extflash = (out / f"{model}-extflash-patched.bin").read_bytes()
            original_extflash = backup_dir / f"flash_backup_{model}.bin"
            asset_size = original_extflash.stat().st_size
            if len(bank1) != 256 * 1024 or len(patched_extflash) < asset_size:
                raise ValueError("stock patch output has an unexpected flash-image size")
            bank1_components = []
            sd_bootloader_info = None
            if args.bootloader:
                if args.bootloader_file:
                    raise ValueError(
                        "--bootloader-file is linked at 0x08000000; use "
                        "--sd-bootloader-file for the Game+Left bootloader at 0x08032000")
                from gwprov.bootloader import resolve_sd_bootloader
                cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                sd_bootloader, sd_bootloader_info = resolve_sd_bootloader(
                    cache=cache_home / "gwprov", repo=args.bootloader_repo,
                    version=args.bootloader_version, local=args.sd_bootloader_file)
                bootloader_offset = 0x32000
                if len(bank1) < 0x40000 or any(byte != 0xFF for byte in bank1[bootloader_offset:0x40000]):
                    raise ValueError(
                        "patched bank 1 does not leave the 0x08032000 bootloader region erased")
                prefix = bank1[:bootloader_offset]
                prefix_path = destination / "blobs" / "stock-bank1-patched.bin"
                bootloader_path = destination / "blobs" / "sd-bootloader.bin"
                prefix_path.write_bytes(prefix)
                bootloader_path.write_bytes(sd_bootloader)
                bank1_components = [
                    {"name": f"Stock {model.title()} (+patch)",
                     "file": prefix_path.relative_to(destination).as_posix(),
                     "offset": 0, "size": len(prefix),
                     "sha256": hashlib.sha256(prefix).hexdigest()},
                    {"name": f"SD bootloader ({sd_bootloader_info.get('version') or 'local'})",
                     "file": bootloader_path.relative_to(destination).as_posix(),
                     "offset": bootloader_offset, "size": len(sd_bootloader),
                     "sha256": sd_bootloader_info["sha256"]},
                ]
                bank1 = prefix + sd_bootloader + b"\xff" * (
                    256 * 1024 - bootloader_offset - len(sd_bootloader))
            else:
                if args.sd_bootloader_file:
                    raise ValueError("--sd-bootloader-file requires --bootloader/-b")
                if args.bootloader_file:
                    raise ValueError(
                        "--bootloader-file is for content-based profiles; it is not used by stock patch profiles")
                bank1_components = [{
                    "name": f"Stock {model.title()} (+patch)",
                    "file": "blobs/stock-bank1.bin", "offset": 0,
                    "size": len(bank1), "sha256": hashlib.sha256(bank1).hexdigest()}]
            # The patcher pads external flash to the selected chip capacity.
            # Keep only the stock asset range in the component map so later
            # Retro-Go extflash components can be mapped at non-overlapping offsets.
            bank0 = patched_extflash[:asset_size]
            (destination / "bank1.bin").write_bytes(bank1)
            (destination / "extflash.bin").write_bytes(
                bank0 + b"\xff" * (args.extflash_mib * 1024 * 1024 - len(bank0)))
            if not args.bootloader:
                (destination / "blobs" / "stock-bank1.bin").write_bytes(bank1)
            (destination / "blobs" / "stock-assets.bin").write_bytes(bank0)
            flash_map_path = destination / "flash-map.yaml"
            flash_map = yaml.safe_load(flash_map_path.read_text())
            flash_map["bank1"] = bank1_components
            flash_map["bank0"] = [{"name": f"{model.title()} Assets",
                                  "file": "blobs/stock-assets.bin", "offset": 0,
                                  "size": len(bank0),
                                  "sha256": hashlib.sha256(bank0).hexdigest()}]
            flash_map_path.write_text(yaml.safe_dump(flash_map, sort_keys=False))
            provision_path = destination / "provision.json"
            provision = json.loads(provision_path.read_text())
            provision["patch"] = {"applied": True, "bootloader": bool(args.bootloader)}
            if sd_bootloader_info is not None:
                provision["patch"]["sdBootloader"] = sd_bootloader_info
            provision_path.write_text(json.dumps(provision, indent=2) + "\n")
            shutil.rmtree(out)
        if args.retro_go:
            import tempfile
            from gwprov.retro_sources import resolve_release
            from gwprov.dist.firmware import install_firmware
            from gwprov.provision import create_profile
            from gwprov.profiles import edit_profile_blob
            import yaml, shutil
            source, tag = resolve_release(args.retro_go)
            with tempfile.TemporaryDirectory(prefix="gwprov-retro-profile-") as work:
                content = Path(work) / "content"
                install_firmware(content, variant="flash", repo=source["repo"], version=tag,
                                 versions_url=source["versionsUrl"])
                assembled = Path(work) / "assembled"
                create_profile(assembled, content=content, littlefs_mib=args.littlefs_mib,
                               extflash_mib=args.extflash_mib)
                mapping = yaml.safe_load((assembled / "flash-map.yaml").read_text())
                # A stock profile's erased-bank placeholder is a real mapped
                # component. Remove it before placing Retro-Go at bank 2.
                stock_map_path = destination / "flash-map.yaml"
                stock_map = yaml.safe_load(stock_map_path.read_text())
                erased = next((item for item in stock_map.get("bank2", [])
                               if item.get("name") == "Erased (0xFF)"), None)
                if erased:
                    edit_profile_blob(destination, action="remove", bank="bank2",
                                      name=erased["name"])
                for bank in ("bank2", "bank0"):
                    for item in mapping.get(bank, []):
                        blob = assembled / item["file"]
                        copied = destination / "blobs" / f"retro-{bank}-{blob.name}"
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(blob, copied)
                        edit_profile_blob(destination, action="add", bank=bank,
                                          name=item["name"], source=copied,
                                          offset=item["offset"])
                assembled_provision = json.loads((assembled / "provision.json").read_text())
                assembled_debug = assembled / "debug"
                if assembled_debug.is_dir():
                    shutil.copytree(assembled_debug, destination / "debug", dirs_exist_ok=True)
                stock_provision_path = destination / "provision.json"
                stock_provision = json.loads(stock_provision_path.read_text())
                stock_provision["retroGo"] = {
                    "firmware": assembled_provision.get("firmware", {}),
                    "debugSymbols": assembled_provision.get("debugSymbols"),
                }
                stock_provision["projects"] = {
                    **stock_provision.get("projects", {}),
                    **assembled_provision.get("projects", {}),
                }
                stock_provision["projectSymbols"] = assembled_provision.get("projectSymbols", [])
                stock_provision["layout"] = {
                    **stock_provision.get("layout", {}),
                    **assembled_provision.get("layout", {}),
                }
                stock_provision_path.write_text(json.dumps(stock_provision, indent=2) + "\n")
        print(f"Created {result['model'].title()} profile: {destination_label}")
        return 0
    content_root = args.content
    if args.retro_go:
        if content_root:
            raise ValueError("--retro-go and --content are mutually exclusive")
        import tempfile
        from gwprov.retro_sources import resolve_release
        from gwprov.dist.firmware import install_firmware
        source, tag = resolve_release(args.retro_go)
        temporary = tempfile.TemporaryDirectory(prefix="gwprov-profile-content-")
        content_root = str(Path(temporary.name) / "content")
        install_firmware(content_root, variant="flash", repo=source["repo"], version=tag,
                         versions_url=source["versionsUrl"])
    if not content_root:
        raise ValueError("profile create requires -m/--mario, -z/--zelda, --retro-go SOURCE:VERSION, or --content DIR")
    if args.patched or args.bootloader:
        raise ValueError("--patched and --bootloader require a Mario or Zelda stock profile")
    from gwprov.provision import create_profile
    report = create_profile(destination, content=content_root, name=args.name,
                            littlefs_mib=args.littlefs_mib, extflash_mib=args.extflash_mib,
                            sd_size_mib=args.sd_size_mib, sd_label=args.sd_label,
                            bootloader_repo=args.bootloader_repo,
                            bootloader_version=args.bootloader_version,
                            bootloader_file=args.bootloader_file)
    if report.get('variant') == 'sd':
        print(f"Created {destination_label}: SD image {report['layout']['sdImageBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    else:
        print(f"Created {destination_label}: FrogFS {report['layout']['frogfsBytes']} bytes; "
              f"LittleFS {report['layout']['littlefsBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    print(f"Boot with: gwprov gwemu run --profile {destination_label}")
    if args.retro_go:
        temporary.cleanup()
    return 0


def _profile_duplicate(args) -> int:
    from gwprov.profiles import duplicate_profile, profile_reference

    result = duplicate_profile(args.source, args.destination, output_dir=args.output_dir)
    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        console.print("[bold green]Profile duplicated[/]")
        console.print(f"  [dim]From[/]  {profile_reference(result['source'])}")
        destination = profile_reference(result['destination'], profile_dir=args.output_dir)
        console.print(f"  [dim]To[/]    [bold cyan]{destination}[/]")
        console.print(f"  [dim]Files[/] {result['files']}")
        console.print(f"\nRun it with [bold]gwprov gwemu run --profile {destination}[/]")
    return 0


def _profile_blob(args) -> int:
    from gwprov.profiles import edit_profile_blob, profile_reference

    result = edit_profile_blob(args.profile, action=args.action, bank=args.bank,
                               name=args.name, source=getattr(args, "file", None),
                               offset=getattr(args, "offset", None),
                               replace=getattr(args, "replace", False))
    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        label = profile_reference(result["profile"])
        if result["action"] == "removed":
            console.print(f"[green]Removed[/] {result['name']} from {label} ({result['bank']})")
        else:
            console.print(f"[green]{result['action'].title()}[/] {result['name']} ({result['bank']}) "
                          f"at 0x{result['offset']:X} ({_human_size(result['size'])})")
            console.print(f"  Profile: {label}")
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
    table = Table(title=f"[bold bright_cyan]Device profiles ({len(rows)})[/]",
                  title_justify="left", expand=True, show_lines=False)
    table.add_column("NAME", style="bold cyan", no_wrap=True)
    table.add_column("STATE", no_wrap=True)
    table.add_column("DEVICE / FIRMWARE", overflow="fold")
    table.add_column("LOCATION", overflow="fold")
    for row in rows:
        details = ", ".join(value for value in (row["model"], row["firmware"]) if value) or "—"
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
    return show_devices(output=args.output, profile=args.profile, refresh=args.refresh,
                        no_pager=args.no_pager)


def _resolve_device_selector(selector: str) -> dict:
    """Resolve a device ID or display/name selector without opening hardware."""
    from gwprov.devices import device_rows

    rows = device_rows()
    folded = selector.casefold()
    matches = [row for row in rows
               if str(row.get("id", "")).casefold() == folded
               or str(row.get("displayName", "")).casefold() == folded
               or str(row.get("name", "")).casefold() == folded]
    if not matches:
        raise ValueError(f"device {selector!r} is not currently available; use `gwprov devices` to list devices")
    if len(matches) > 1:
        choices = ", ".join(str(row["id"]) for row in matches)
        raise ValueError(f"device name {selector!r} is ambiguous; select one by ID: {choices}")
    return matches[0]


def _device_config(args) -> int:
    from gwprov.device_config import show_device_config
    return show_device_config(output=args.output)


def _retro_go_config(args) -> int:
    """Read or modify Retro-Go /CONFIG with inline options.

    A profile target changes the managed image.  A device target changes its
    assigned profile and deploys only the LittleFS region after confirmation.
    With no mutation flags the operation is read-only.
    """
    from gwprov.common.retrogo_config import build_from_values, rom_path
    from gwprov.device_config import decode_config, read_config
    from gwprov.profiles import DeviceProfile, profile_reference

    if args.device and args.profile:
        raise ValueError("choose a device or --profile, not both")
    if not args.device and not args.profile:
        raise ValueError("retro-go config requires DEVICE or --profile PROFILE")
    if args.rom and (args.auto_start is not None or args.no_auto_start):
        raise ValueError("--rom cannot be combined with --auto-start or --no-auto-start")
    target_row = _resolve_device_selector(args.device) if args.device else None
    device_id = target_row["id"] if target_row else None
    assigned_profile = (target_row or {}).get("assignedProfile") if target_row else None
    profile_name = args.profile or assigned_profile
    mutation_requested = any((args.auto_start is not None, args.no_auto_start,
                              args.timeout is not None, args.oc_level is not None,
                              args.selected_tab is not None, args.cursor is not None,
                              args.browse_subpath is not None))
    if device_id and not profile_name and mutation_requested:
        raise ValueError("device has no assigned profile; use --profile PROFILE for the update")

    blob, source, _ = read_config(device_id=device_id, profile=profile_name,
                                  remote_origin=(target_row or {}).get("remoteOrigin"))
    decoded = decode_config(blob)
    values = decoded["values"]
    startup_file = None
    if args.rom:
        startup_file = rom_path(*args.rom)
    elif args.auto_start is not None:
        startup_file = args.auto_start
    elif args.no_auto_start:
        startup_file = ""
    overrides = {
        "startup_file": startup_file,
        "menu_timeout_s": args.timeout,
        "cpu_oc_level": args.oc_level,
        "selected_tab": args.selected_tab,
        "cursor": args.cursor,
        "browse_subpath": args.browse_subpath,
    }
    mutations = {key: value for key, value in overrides.items() if value is not None}
    if not mutations:
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(blob)
        return _print_config_report(decoded, source=source, target=device_id or profile_name,
                                    output_format=args.format)

    if "startup_file" in mutations and len(mutations["startup_file"]) >= 256:
        raise ValueError("--auto-start path must be shorter than 256 bytes")
    if "menu_timeout_s" in mutations and mutations["menu_timeout_s"] < 0:
        raise ValueError("--timeout must be nonnegative")
    if "cpu_oc_level" in mutations and not 0 <= mutations["cpu_oc_level"] <= 3:
        raise ValueError("--oc-level must be between 0 and 3")
    new_blob = build_from_values(values, **mutations)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(new_blob)

    if profile_name:
        with tempfile.NamedTemporaryFile(prefix="gwprov-config-", suffix=".bin", delete=False) as handle:
            temp_path = Path(handle.name)
            handle.write(new_blob)
        try:
            from gwprov.filesystem import operate
            operate(operation="add", target="flash/ext", profile=profile_name,
                    filesystem="littlefs", path="CONFIG", source=str(temp_path))
        finally:
            temp_path.unlink(missing_ok=True)
    if device_id:
        if target_row.get("kind") != "hardware":
            raise ValueError("writing Retro-Go CONFIG to GWemu is not supported; update its profile instead")
        _confirm_apply(args, bool(args.yes), [
            f"Target: {target_row.get('displayName') or target_row.get('name') or device_id}",
            f"Profile: {profile_reference(DeviceProfile.load(profile_name).root)}",
            "Action: write /CONFIG and deploy the LittleFS region",
        ])
        from gwprov.deploy import apply_deployment
        remote_url = device_id.removeprefix("remote:") if device_id.startswith("remote:") else None
        apply_deployment(profile_name, probe_id=target_row.get("probeId"),
                         remote_url=remote_url,
                         remote_origin=target_row.get("remoteOrigin"),
                         regions=["littlefs"])
    return _print_config_report(decode_config(new_blob), source=source,
                                target=device_id or profile_name,
                                output_format=args.format)


def _print_config_report(decoded: dict, *, source: str, target: str,
                         output_format: str) -> int:
    report = {"schemaVersion": 1, "target": target, "source": source,
              **decoded}
    valid = all(decoded["integrity"][key]
                for key in ("magic_valid", "version_supported", "crc_valid"))
    if output_format == "json":
        print(json.dumps(report, indent=2))
        return 0 if valid else 1
    from rich.console import Console
    from rich.table import Table
    table = Table(title=f"Retro-Go /CONFIG: {target}", title_justify="left", expand=True)
    table.add_column("SETTING", style="cyan", overflow="fold")
    table.add_column("VALUE", overflow="fold")
    for key, value in decoded["values"].items():
        if key == "start_action":
            value = {0: "resume", 1: "new game"}.get(value, f"unknown ({value})")
        table.add_row(key.replace("_", " "), str(value))
    Console().print(table)
    Console().print(f"Source: {source}")
    return 0 if valid else 1


def _device_recover(args) -> int:
    from gwprov.active_device import get_active, get_active_origin

    device_id = get_active()
    if not device_id:
        raise ValueError("no active device; select one with `gwprov set active DEVICE`")
    if device_id.startswith("probe:"):
        from gwprov.backends import SelectedPyOCDBackend
        backend = SelectedPyOCDBackend(device_id.removeprefix("probe:"),
                                       operation="gwprov device recover", lease_wait=0,
                                       allow_recovery_marker=True,
                                       resume_on_disconnect=False)
    elif device_id.startswith("remote:"):
        from gwprov.backends import WebSocketBackend
        backend = WebSocketBackend(device_id.removeprefix("remote:"),
                                   origin=get_active_origin(),
                                   operation="gwprov device recover", lease_wait=0,
                                   allow_recovery_marker=True)
    else:
        raise ValueError("device recover requires an active physical device")

    from gwprov.hw_lifecycle import recover_flasher
    try:
        backend.open()
        from gnwmanager.gnw import GnW
        state = recover_flasher(backend, GnW(backend))
    finally:
        backend.close()
    action = "started" if state["started"] else "attached to"
    print(f"Recovery flasher {action}; ready for commands")
    print(f"  Context: {state['contextCounter']}  VTOR: 0x{state['vtor']:08x}")
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
    field = args.set_command
    if args.value is None:
        value = get_assignment(device).get(field)
        label = "display name" if field == "name" else field
        if field == "name" and not value:
            from gwprov.devices import device_rows
            row = next((row for row in device_rows() if row["id"] == device), None)
            default = row.get("displayName") if row else None
            print(f"Active device {device} display name: default ({default or 'unknown'})")
        else:
            print(f"Active device {device} {label}: {value or 'not set'}")
        return 0
    if args.value.casefold() in {"none", "clear"}:
        set_assignment(device, field, None)
        label = "display name" if field == "name" else f"{field} assignment"
        print(f"Cleared {label} for {device}")
        return 0
    if field == "name":
        value = args.value.strip()
        if not value or any(char in value for char in "\r\n\0"):
            raise ValueError("device name must be a non-empty single-line value")
    elif field == "profile":
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
    label = "display name" if field == "name" else field
    print(f"Set {label} for {device}: {value}")
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
    assume_yes = bool(getattr(args, "yes", False))
    from gwprov.active_device import get_active, get_active_origin, set_active
    from gwprov.device_assignments import get_assignment, set_assignment
    from gwprov.devices import device_rows
    from gwprov.profiles import DeviceProfile, profile_reference

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
        if args.start:
            raise ValueError("--start and --bank apply only to hardware deployments")
        from gwprov.gwemu_manager import stop_instance
        from gwprov.daemon import start_instance
        current_profile = row.get("profile")
        if current_profile == str(profile.root) and not assignment.get("sdcard"):
            if output in {"json", "yaml"}:
                _print_structured({"device": device_id, "profile": str(profile.root),
                                   "status": "already-applied"}, output)
            else:
                print(f"GWemu pid {row['pid']} already uses profile {profile_reference(profile.root)}.")
            return 0
        if row.get("status") == "unknown":
            raise ValueError("cannot apply to a GWemu instance with unknown execution state; inspect `gwprov ps`")
        _confirm_apply(args, assume_yes, [
            f"Profile: {profile_reference(profile.root)}",
            f"Target: GWemu pid {row['pid']} ({row.get('status', 'unknown')})",
            "Action: launch or select the profile instance, then stop the currently selected instance",
        ])
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
        if output in {"json", "yaml"}:
            _print_structured({"device": new_id, "profile": str(profile.root),
                               "status": "applied"}, output)
        else:
            print(f"Applied profile {profile_reference(profile.root)} to GWemu; active device is {new_id}.")
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
    card = None
    if card_name and profile.resolved_sd:
        from gwprov.sd_cards import list_cards
        card = next((item for item in list_cards()
                     if item["name"].casefold() == card_name.casefold()), None)
        if card is None:
            raise ValueError(f"assigned SD card {card_name!r} is no longer registered")
        from gwprov.deploy import deployment_plan
        write_plan = deployment_plan(str(profile.root))
        planned_regions = write_plan["regions"]
        regions = [item["region"] for item in planned_regions if item["region"] != "sd"]
    else:
        from gwprov.deploy import deployment_plan
        write_plan = deployment_plan(str(profile.root))
        planned_regions = write_plan["regions"]
        regions = [item["region"] for item in planned_regions]
    if args.start and not regions:
        raise ValueError("--start requires at least one hardware flash region in the profile")
    from gwprov.deploy import deployment_write_summary
    hardware_plan = {**write_plan,
                     "regions": [item for item in planned_regions if item["region"] in regions]}
    write_rows = deployment_write_summary(str(profile.root), hardware_plan)
    operation = [
        f"Profile: {profile_reference(profile.root)}",
        f"Target: Hardware ({device_id})",
        (f"After writing: start bank {args.bank} reset vector" if args.start
         else "After writing: leave the RAM flasher running"),
    ]
    if card is not None:
        operation.extend((f"SD card: {card['name']}", f"SD card path: {card['path']}",
                          "SD card action: retain existing files and replace matching files"))
    _confirm_apply(args, assume_yes, operation, writes=write_rows)
    if card is not None:
        from gwprov.deploy import overlay_sd_directory
        sd_written = overlay_sd_directory(profile.resolved_sd, Path(card["path"]))
    if regions == []:
        result = {"profile": str(profile.root), "written": [
            {"region": "sd", "files": sd_written, "mode": "mounted-folder-overlay"}
        ]}
        if output in {"json", "yaml"}:
            _print_structured(result, output)
        else:
            print(f"Updated SD card with {sd_written} file(s).", flush=True)
        return 0
    from gwprov.deploy import apply_deployment
    callback, finish = _deployment_progress_display(output)
    try:
        result = apply_deployment(str(profile.root), regions=regions,
                                  start=args.start, bank=args.bank,
                                  progress=callback, **target)
    finally:
        if finish:
            finish()
    if sd_written is not None:
        result["written"].append({"region": "sd", "files": sd_written,
                                  "mode": "mounted-folder-overlay"})
    if output in {"json", "yaml"}:
        _print_structured(result, output)
    return 0


def _apply_parser(args) -> int:
    return _apply_assigned(args)


def _confirm_apply(args, assume_yes: bool, details: list[str], *, writes=None) -> None:
    """Require an explicit go-ahead before apply changes a device or files."""
    if assume_yes:
        return
    from rich.prompt import Confirm

    console = Console(file=sys.stderr, force_terminal=sys.stderr.isatty())
    console.print(_apply_plan_renderable(details, writes or [], console.width))
    if not sys.stdin.isatty():
        raise ValueError("confirmation needs an interactive terminal; rerun with `gwprov apply --yes`")
    try:
        accepted = Confirm.ask("Continue?", default=False, console=console)
    except EOFError as exc:
        raise ValueError("apply was not confirmed; rerun with `gwprov apply --yes` to skip the prompt") from exc
    if not accepted:
        raise ValueError("apply cancelled")


def _apply_plan_renderable(details: list[str], writes: list[dict], width: int):
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    summary = []
    for detail in details:
        label, separator, value = detail.partition(": ")
        text = Text()
        if separator:
            text.append(f"{label:<10} ", style="dim")
            text.append(value)
        else:
            text.append(detail)
        summary.append(text)
    renderables = list(summary)
    if writes:
        if width >= 92:
            table = Table(box=box.SIMPLE, expand=True, padding=(0, 1),
                          show_header=True, header_style="bold")
            table.add_column("FLASH TARGET", style="bright_cyan", overflow="fold")
            table.add_column("IMAGE", overflow="fold")
            table.add_column("SIZE", justify="right", no_wrap=True)
            for row in writes:
                table.add_row(Text(str(row["target"])), Text(str(row["content"])),
                              _human_size(row["bytes"]) if row["bytes"] is not None else "device read")
        else:
            table = Text()
            for index, row in enumerate(writes):
                if index:
                    table.append("\n")
                table.append(str(row["target"]), style="bold bright_cyan")
                table.append("\n  ")
                table.append(str(row["content"]))
                table.append("\n  Size: ", style="dim")
                table.append(_human_size(row["bytes"]) if row["bytes"] is not None else "read from device",
                             style="dim")
        renderables.extend([Text("Planned writes", style="bold"), table])
    return Panel(Group(*renderables), title="Apply plan", border_style="bright_cyan",
                 box=box.ROUNDED, expand=True)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{int(value)} {unit}" if unit == "bytes" else f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024


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
    table.add_column("DETAILS", overflow="fold")
    for row in rows:
        table.add_row("●" if row["id"] == active else "", row["name"], row["adapter"],
                      row["id"], row["state"].upper(), row.get("detail") or "")
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
            print(json.dumps([{**{k: row.get(k) for k in ("id", "kind", "name", "status", "application", "installed",
                                                              "assignedProfile", "sdCard")},
                               "active": row["id"] == active} for row in rows], indent=2))
            return 0
        if not rows:
            Console().print("[dim]No devices detected. Connect a probe or start GWemu, then run `gwprov show devices` again.[/]")
            return 0
        table = Table(title="Devices", title_justify="left", expand=True)
        table.add_column("", width=1, no_wrap=True)
        for name in ("DEVICE", "TYPE", "STATE", "RUNNING", "INSTALLED", "PROFILE", "SD CARD"):
            table.add_column(name, overflow="fold")
        for row in rows:
            state = Text(str(row["status"]).upper(), style={"running": "green", "ready": "green",
                         "halted": "yellow", "recovery": "yellow",
                         "busy": "cyan", "unknown": "red"}.get(row["status"], "white"))
            table.add_row(Text("●" if row["id"] == active else "", style="bold cyan"),
                          row["id"], row["kind"], state, row.get("application", "Unknown"),
                          row.get("installed", "Unknown"),
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


def _daemon_restart(args) -> int:
    from gwprov.daemon_ipc import request
    from gwprov.daemon import (ensure_running, _stop_stale_daemon,
                               _daemon_identity, _pid_exists)
    try:
        ping = request({"op": "ping", "clientIdentity": _daemon_identity()}, timeout=1.0)
    except (OSError, RuntimeError, ValueError):
        ping = None
    if ping is None:
        ensure_running(timeout=args.timeout)
        print("GWProv daemon started")
        return 0

    stale = bool(ping.get("reloadRequired"))
    if stale:
        if int(ping.get("activeVMs", 0)):
            raise RuntimeError("daemon owns active GWemu instances; stop them before restarting it")
        from gwprov.target_leases import active_lease_owners, operation_blocks_drain
        blocking_owners = [owner for owner in active_lease_owners()
                           if operation_blocks_drain(owner)]
        cancelled = []
        if blocking_owners and not args.force:
            raise RuntimeError(
                f"stale daemon has {len(blocking_owners)} substantial device operation(s); "
                "wait for them or use `gwprov daemon restart --force` to cancel them")
        if args.force:
            for owner in blocking_owners:
                pid = owner.get("pid")
                if isinstance(pid, int) and pid > 1 and _pid_exists(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        cancelled.append(pid)
                    except ProcessLookupError:
                        pass
            grace = time.monotonic() + 2.0
            while time.monotonic() < grace and any(_pid_exists(pid) for pid in cancelled):
                time.sleep(0.05)
            for pid in cancelled:
                if _pid_exists(pid) and os.name != "nt":
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            deadline_cancel = time.monotonic() + 1.0
            while time.monotonic() < deadline_cancel and any(_pid_exists(pid) for pid in cancelled):
                time.sleep(0.05)
            if any(_pid_exists(pid) for pid in cancelled):
                raise RuntimeError("could not cancel every stale device operation owner")
        _stop_stale_daemon(int(ping.get("daemonPid", 0)),
                           timeout=min(args.timeout, 8.0), force=args.force)
        result = {"cancelledPids": sorted(set(cancelled))}
    else:
        operation = "daemon-force-drain" if args.force else "daemon-drain"
        result = request({"op": operation}, timeout=3.0)
    cancelled = result.get("cancelledPids", [])
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        try:
            request({"op": "hello"}, timeout=0.25)
        except (OSError, RuntimeError, ValueError):
            break
        time.sleep(0.05)
    else:
        if args.force:
            raise RuntimeError("daemon is still draining; a device owner did not exit after cancellation")
        raise RuntimeError(
            f"daemon is draining with {result.get('blockingLeases', 0)} substantial device operation(s); "
            "use `gwprov daemon restart --force` to cancel them")
    ensure_running(timeout=args.timeout)
    if cancelled:
        print(f"GWProv daemon restarted; canceled process(es): {', '.join(map(str, cancelled))}")
    else:
        print("GWProv daemon restarted")
    return 0


def _deploy_plan(args) -> int:
    from gwprov.deploy import deployment_plan
    plan = deployment_plan(args.profile, args.region)
    if args.output in {"json", "yaml"}:
        _print_structured(plan, args.output)
    else:
        from gwprov.profiles import profile_reference
        print(f"Deployment plan: {profile_reference(plan['profile'])}")
        for row in plan["regions"]:
            destination = f"bank {row['bank']}+0x{row['offset']:x}" if "bank" in row else f"offset 0x{row['offset']:x}"
            print(f"  {row['region']:<10} {row['bytes']:>10} bytes  {destination}  sha256={row['sha256']}")
        print("Apply leaves the RAM flasher running unless --start is given.")
        print("Use --start --bank 1 (default) or --start --bank 2 to run a bank after writing.")
        print("SD deployment overlays files and retains existing files.")
    return 0


def _print_structured(value, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps(value, indent=2))
    elif output_format == "yaml":
        import yaml
        print(yaml.safe_dump(value, sort_keys=False), end="")
    else:
        raise ValueError(f"unsupported structured output format: {output_format}")


def _deployment_progress_display(output: str):
    """Keep live diagnostics off stdout when a structured result is requested."""
    if output in {"json", "yaml"}:
        diagnostic = Console(file=sys.stderr, force_terminal=sys.stderr.isatty())

        def report_json_progress(event: dict) -> None:
            if event["event"] == "device_extflash":
                diagnostic.print(_device_extflash_renderable(event["summary"], diagnostic.width))

        return report_json_progress, None
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
                if task_id is None:
                    task_id = progress.add_task("Preparing deployment", total=event["total_bytes"])
                else:
                    progress.update(task_id, total=event["total_bytes"], completed=0,
                                    description="Preparing deployment")
            elif kind == "device_extflash":
                progress.console.print(_device_extflash_renderable(event["summary"], console.width))
            elif kind == "phase" and task_id is not None:
                progress.update(task_id, description=event["message"])
            elif kind == "region_start" and task_id is not None:
                progress.update(task_id, description=f"Writing {event['region']}")
            elif kind == "progress" and task_id is not None:
                progress.update(task_id, completed=event["overall_done"])
            elif kind == "complete" and task_id is not None:
                progress.update(task_id, completed=event["overall_total"],
                                description=(f"Started {event['booted']}" if event.get("booted")
                                             else "RAM flasher ready"))

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
        elif kind == "device_extflash":
            console.print(_device_extflash_renderable(event["summary"], console.width))
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
            if event.get("booted"):
                print(f"Deployment complete; started {event['booted']}.", flush=True)
            else:
                print("Deployment complete; RAM flasher remains running.", flush=True)

    return update, None


def _device_extflash_renderable(summary: dict, width: int):
    from rich import box
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    detail = Text(f"Detected capacity: {_human_size(summary['size'])}")
    if summary.get("model"):
        detail.append(f"\nDevice model: {summary['model']}", style="dim")
    if width >= 92:
        table = Table(box=box.SIMPLE, expand=True, padding=(0, 1), show_header=True,
                      header_style="bold")
        table.add_column("DEVICE RANGE", style="bright_cyan", overflow="fold")
        table.add_column("CURRENT CONTENT", overflow="fold")
        table.add_column("SIZE", justify="right", no_wrap=True)
        for row in summary["regions"]:
            table.add_row(Text(row["target"]), Text(row["content"]),
                          _human_size(row["bytes"]))
    else:
        table = Text()
        for index, row in enumerate(summary["regions"]):
            if index:
                table.append("\n")
            table.append(row["target"], style="bold bright_cyan")
            table.append("\n  ")
            table.append(row["content"])
            table.append("\n  Size: ", style="dim")
            table.append(_human_size(row["bytes"]), style="dim")
    return Panel(Group(detail, Text("Current extflash contents", style="bold"), table),
                 title="Read from device", border_style="bright_cyan",
                 box=box.ROUNDED, expand=True)


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
                                  regions=args.region, start=args.start, bank=args.bank,
                                  progress=callback)
    finally:
        if finish:
            finish()
    if args.output in {"json", "yaml"}:
        _print_structured(result, args.output)
    return 0


def _profile_hardware(args) -> int:
    from gwprov.hw_profile import profile_hardware
    probe_id, programmer, remote_url = args.probe_id, args.programmer, args.remote_url
    remote_origin = args.remote_origin
    target_row = None
    if not any((probe_id, programmer, remote_url)):
        from gwprov.active_device import get_active
        from gwprov.devices import device_rows
        selected_id = get_active()
        target_row = next((item for item in device_rows()
                           if item["id"] == selected_id), None) if selected_id else None
        if target_row is None:
            raise ValueError("select a physical device with `gwprov set active DEVICE` or specify a target")
        if target_row.get("probeId"):
            probe_id = target_row["probeId"]
        elif target_row["id"].startswith("remote:"):
            remote_url = target_row["id"].removeprefix("remote:")
            from gwprov.active_device import get_active_origin
            remote_origin = remote_origin or get_active_origin()
        else:
            raise ValueError("active device is not a hardware target")
    elif probe_id or remote_url:
        from gwprov.devices import device_rows
        target_id = f"probe:{probe_id}" if probe_id else f"remote:{remote_url}"
        target_row = next((item for item in device_rows()
                           if item["id"] == target_id), None)
    elif programmer:
        from gwprov.devices import device_rows
        from gwprov.target_leases import adapter_for_probe
        matches = [item for item in device_rows()
                   if item.get("kind") == "hardware"
                   and str(item.get("id", "")).startswith("probe:")
                   and adapter_for_probe(f"{item.get('vendor', '')} {item.get('name', '')}")
                   == programmer]
        if len(matches) == 1:
            target_row = matches[0]

    profile = args.profile
    if target_row and not profile:
        import re
        from gwprov.debug_symbols import retro_go_profile_symbols
        match = re.search(r"Retro-Go\s+(?:SD\s+)?(v[\w.+-]+)",
                          target_row.get("installed", ""), re.IGNORECASE)
        matched = retro_go_profile_symbols(match.group(1)) if match else None
        if matched:
            firmware_elf, _ = matched
            profile = str(firmware_elf.parents[1])
            import sys
            print(f"Using exact Retro-Go {match.group(1)} symbols from profile "
                  f"{firmware_elf.parents[1].name}", file=sys.stderr)
    return profile_hardware(probe_id=probe_id, programmer=programmer,
                            remote_url=remote_url,
                            remote_origin=remote_origin, profile=profile,
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

    if args.autostart_timeout is not None and args.autostart_timeout < 0:
        raise ValueError("--autostart-timeout must be nonnegative")

    symbol_paths = list(args.symbols)
    qmp_socket = args.qmp_socket
    profile = args.profile
    probe_ids = list(args.probe_id)
    programmers = list(args.programmer)
    remote_urls = list(args.remote_url)
    remote_origins = list(args.remote_origin)
    detected_retro_go = None
    active_row = None
    target = args.target
    gdb_port = args.port
    if args.device:
        if any((probe_ids, programmers, remote_urls)):
            raise ValueError("use either the positional device or --probe-id/--programmer/--remote-url")
        from gwprov.devices import device_rows
        rows = device_rows()
        exact_id = [item for item in rows if item["id"].casefold() == args.device.casefold()]
        exact_name = [item for item in rows if item.get("name", "").casefold() == args.device.casefold()]
        matches = exact_id or exact_name
        if not matches:
            raise ValueError(f"device {args.device!r} is not currently available; use `gwprov devices` to list devices")
        if len(matches) > 1:
            choices = ", ".join(item["id"] for item in matches)
            raise ValueError(f"device name {args.device!r} is ambiguous; select one by ID: {choices}")
        active_row = matches[0]
        inferred_target = "gwemu" if active_row.get("kind") == "gwemu" else "hardware"
        if target and target != inferred_target:
            raise ValueError(f"device {args.device!r} is a {inferred_target} target, not {target}")
        target = inferred_target
    elif target is None:
        from gwprov.active_device import get_active
        from gwprov.devices import device_rows
        selected_id = get_active()
        active_row = next((item for item in device_rows() if item["id"] == selected_id), None) if selected_id else None
        if active_row is None:
            raise ValueError("select a device with `gwprov set active DEVICE` or provide a device name")
        target = "gwemu" if active_row.get("kind") == "gwemu" else "hardware"

    if target == "hardware" and not any((probe_ids, programmers, remote_urls)):
        if active_row is None:
            from gwprov.active_device import get_active
            from gwprov.devices import device_rows
            selected_id = get_active()
            active_row = next((item for item in device_rows() if item["id"] == selected_id), None) if selected_id else None
        if active_row is None:
            raise ValueError("select a physical device with `gwprov set active DEVICE` or provide a device name")
        if active_row.get("probeId"):
            probe_ids.append(active_row["probeId"])
        elif active_row["id"].startswith("remote:"):
            remote_urls.append(active_row["id"].removeprefix("remote:"))
            from gwprov.active_device import get_active_origin
            origin = get_active_origin()
            if origin:
                remote_origins.append(origin)
        else:
            raise ValueError("active device is not a hardware target")
        import re
        match = re.search(r"Retro-Go\s+(?:SD\s+)?(v[\w.+-]+)", active_row.get("installed", ""), re.IGNORECASE)
        if match:
            detected_retro_go = match.group(1)
            if not profile:
                # Use symbols already present in a managed profile for the exact
                # scanned firmware version. Never substitute symbols from another
                # Retro-Go release when there is no exact match.
                from gwprov.debug_symbols import retro_go_profile_symbols
                matched = retro_go_profile_symbols(detected_retro_go)
                if matched:
                    firmware_elf, app_elfs = matched
                    matched_symbols = [str(firmware_elf), *(str(path) for path in app_elfs)]
                    symbol_paths.extend(path for path in matched_symbols
                                        if path not in symbol_paths)
                    print(f"Using installed Retro-Go {detected_retro_go} symbols from profile "
                          f"{firmware_elf.parents[1].name}")
    if target == "gwemu" and qmp_socket is None and not profile:
        from gwprov.active_device import get_active
        if active_row and active_row.get("kind") == "gwemu":
            from gwprov.gwemu_manager import instances
            vm = next((item for item in instances()
                       if str(item.get("pid")) == str(active_row.get("pid"))), None)
            if vm:
                qmp_socket = vm.get("qmpSocket")
                profile = vm.get("profile") or active_row.get("assignedProfile")
                if gdb_port is None:
                    gdb_port = vm.get("gdbPort")
        active_id = get_active()
        selected_is_active = (active_row is None or active_row.get("id") == active_id)
        if qmp_socket is None and profile is None and active_id and selected_is_active:
            selected = _active_gwemu()
            qmp_socket = selected.get("qmpSocket")
            profile = selected.get("profile")
            if qmp_socket is None:
                raise ValueError("active GWemu device has no QMP endpoint")
    if profile:
        from gwprov.profiles import DeviceProfile
        profile_root = DeviceProfile.load(profile).root
        firmware_symbols = profile_root / "debug" / "retro-go-debug.elf"
        if firmware_symbols.is_file():
            symbol_paths.insert(0, str(firmware_symbols))
        debug_root = profile_root / "debug"
        app_symbols = sorted((debug_root / "apps").rglob("*.elf")) if (debug_root / "apps").is_dir() else []
        project_symbols = sorted((debug_root / "projects").rglob("*.elf")) if (debug_root / "projects").is_dir() else []
        symbol_paths[1:1] = [str(path) for path in [*app_symbols, *project_symbols]
                             if str(path) not in symbol_paths]
        if not firmware_symbols.is_file() and not app_symbols and not project_symbols and not symbol_paths:
            raise ValueError(f"profile has no bundled debug symbols: {debug_root}")
        if target == "gwemu" and qmp_socket is None:
            from gwprov.gwemu_manager import instances
            matches = [row for row in instances() if row.get("profile") == str(profile_root)]
            if len(matches) == 1:
                qmp_socket = matches[0].get("qmpSocket")
                if gdb_port is None:
                    gdb_port = matches[0].get("gdbPort")
            elif len(matches) > 1:
                raise ValueError("profile has multiple GWemu instances; select a QMP handle explicitly")
    elif active_row and active_row.get("assignedProfile"):
        from gwprov.profiles import DeviceProfile
        assigned_root = DeviceProfile.load(active_row["assignedProfile"]).root
        project_root = assigned_root / "debug" / "projects"
        if project_root.is_dir():
            for path in sorted(project_root.rglob("*.elf")):
                if str(path) not in symbol_paths:
                    symbol_paths.append(str(path))
    if target == "gwemu" and gdb_port is None:
        raise ValueError("GWemu GDB port was not supplied and could not be inferred; "
                         "pass --port or select a managed GWemu instance")
    return python_shell(target=target, host=args.host, port=gdb_port,
                        openocd_port=args.openocd_port, probe_ids=probe_ids,
                        programmers=programmers, remote_urls=remote_urls,
                        remote_origins=remote_origins,
                        symbols=symbol_paths,
                        qmp_socket=qmp_socket, debug_config=args.debug_config,
                        retro_go_version=detected_retro_go,
                        start_paused=args.paused,
                        attach_only=args.attach_only,
                        autostart_timeout=args.autostart_timeout,
                        watchdog_freeze=not args.no_watchdog_freeze)


def _ofw_patch(args) -> int:
    from gwprov.vendor.qemu_gnw import cfw_images

    forwarded = [
        args.game,
        "--backup-dir", args.backup_dir,
        "--output-dir", args.output_dir,
        "--extflash-mib", str(args.extflash_mib),
        *( ["--no-bootloader"] if args.no_bootloader else [] ),
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
                   source=getattr(args, "source", None),
                   mapped_sources=getattr(args, "mapped_source", []),
                   size_mib=size_mib, force=getattr(args, "force", False))


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


def _infer_project_variant_from_directory(directory: str | Path | None) -> str | None:
    if not directory:
        return None
    root = Path(directory).expanduser()
    metadata = root / ".gwprov-firmware.json"
    if metadata.is_file():
        try:
            value = json.loads(metadata.read_text(encoding="utf-8")).get("variant")
        except (OSError, ValueError, AttributeError):
            value = None
        if value in {"flash", "sd"}:
            return value
    if root.name.casefold() in {"flash", "sd"}:
        return root.name.casefold()
    present = {name for name in ("flash", "sd") if (root / name).is_dir()}
    if len(present) == 1:
        return present.pop()
    project_markers = []
    for name in ("flash", "sd"):
        marker = root / name / ".gwprov-projects.json"
        if marker.is_file():
            project_markers.append(name)
    if len(project_markers) == 1:
        return project_markers[0]
    return None


def _infer_project_variant_from_profile(profile_root: str | Path) -> str:
    root = Path(profile_root)
    provision_path = root / "provision.json"
    provision = {}
    if provision_path.is_file():
        try:
            provision = json.loads(provision_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ValueError(f"cannot read profile provisioning metadata: {provision_path}: {error}") from error
        if not isinstance(provision, dict):
            raise ValueError(f"invalid profile provisioning metadata: {provision_path}")
    candidates = [provision.get("variant")]
    retro = provision.get("retroGo")
    if isinstance(retro, dict) and isinstance(retro.get("firmware"), dict):
        candidates.append(retro["firmware"].get("variant"))
    firmware = provision.get("firmware")
    if isinstance(firmware, dict):
        candidates.append(firmware.get("variant"))
    declared = {value for value in candidates if value in {"flash", "sd"}}
    if len(declared) > 1:
        raise ValueError(f"profile has conflicting project variants: {', '.join(sorted(declared))}")
    if declared:
        return declared.pop()
    try:
        from gwprov.profiles import DeviceProfile
        profile = DeviceProfile.load(root)
    except (OSError, RuntimeError, ValueError):
        profile = None
    if profile and profile.sd_mode in {"bundled", "shared"}:
        return "sd"
    layout = provision.get("layout", {})
    if isinstance(layout, dict) and (layout.get("frogfsBytes") or layout.get("littlefsBytes")):
        return "flash"
    # Profiles without an SD image target flash by convention. Explicit project
    # storage metadata still rejects this choice if the release is SD-only.
    return "flash"


def _retain_profile_mapped_sources(profile_root: Path, staged_frogfs: Path,
                                  ownership: dict, prior_projects: dict) -> dict[str, str]:
    """Keep the original mapped bytes needed to recompose a profile's FrogFS."""
    new_keys = tuple(ownership)
    project_records = dict(prior_projects)
    project_records.update(ownership)
    sources: dict[str, str] = {}
    for key, project in project_records.items():
        if not isinstance(project, dict):
            continue
        for index, mapped in enumerate(project.get("mapped", [])):
            if not isinstance(mapped, dict) or not isinstance(mapped.get("path"), str):
                raise ValueError(f"invalid mapped XiP metadata in project {key!r}")
            relative = Path(mapped["path"])
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise ValueError(f"invalid mapped XiP path in project {key!r}: {relative}")
            raw_source = mapped.get("source")
            if raw_source:
                source = (profile_root / raw_source).resolve()
                if profile_root.resolve() not in source.parents or not source.is_file():
                    raise ValueError(f"mapped XiP source is missing from profile {profile_root.name}: {raw_source}")
            else:
                source = staged_frogfs / relative
                if not source.is_file():
                    source = None
                    from gwprov.profiles import DeviceProfile
                    profile = DeviceProfile.load(profile_root)
                    metadata_path = profile_root / "provision.json"
                    profile_metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
                    layout = profile_metadata.get("layout", {})
                    offset = int(layout.get("frogfsOffset", -1))
                    size = int(layout.get("frogfsBytes", 0))
                    raw_base = mapped.get("relocBase")
                    try:
                        reloc_base = int(raw_base, 0) if isinstance(raw_base, str) else int(raw_base)
                    except (TypeError, ValueError) as exc:
                        raise ValueError(f"mapped XiP path {relative} has no valid relocBase") from exc
                    if offset < 0 or size <= 0 or not profile.extflash.is_file():
                        raise ValueError(f"profile has no readable FrogFS image for mapped XiP path {relative}")
                    from gwprov.vendor.retrogo_sd.scripts.frogfs_pico8_ro import (
                        find_uncompressed_file_payload, patch_mapped_bytes)
                    extflash = profile.extflash.read_bytes()
                    image = extflash[offset:offset + size]
                    found = find_uncompressed_file_payload(image, relative.as_posix())
                    if not found:
                        raise ValueError(f"mapped XiP payload is missing or compressed in FrogFS: {relative}")
                    payload_offset, payload_size = found
                    target_address = 0x90000000 + offset + payload_offset
                    relocated = image[payload_offset:payload_offset + payload_size]
                    raw, _count = patch_mapped_bytes(relocated, reloc_base,
                                                     target_address)
                import hashlib
                identity = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
                stored = Path("blobs/project-mapped") / identity / f"{index}-{relative.name}"
                destination = profile_root / stored
                destination.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                if source is not None:
                    shutil.copyfile(source, destination)
                else:
                    destination.write_bytes(raw)
                mapped["source"] = stored.as_posix()
                source = destination
            if relative.as_posix() in sources:
                raise ValueError(f"multiple projects map the same FrogFS path: {relative}")
            sources[relative.as_posix()] = str(source)
    for key in new_keys:
        ownership[key] = project_records[key]
    return sources


def _project_install(args) -> int:
    from gwprov.dist import install_project
    from gwprov.dist.project import _project_reference

    bundle_path = None
    candidate = Path(args.repo).expanduser()
    if candidate.suffix.casefold() == ".zip":
        if not candidate.is_file():
            raise ValueError(f"project bundle ZIP does not exist: {candidate}")
        bundle_path = str(candidate.resolve())
        name, versions_url = str(candidate), None
    else:
        name, versions_url = _project_reference(args.repo)
    output = args.output
    install_root = None
    profile_root = None
    target_device = None
    target_card = None
    assigned_card_name = None
    profile_name = args.profile
    variant_hint = None
    if args.device:
        from gwprov.active_device import get_active
        from gwprov.device_assignments import get_assignment
        device_id = get_active() if args.device == "active" else args.device
        if not device_id:
            raise ValueError("no active device; select one with `gwprov set active DEVICE`")
        if args.dry_run:
            # A plan must stay offline: do not start the daemon or open a debug
            # session just to decorate the destination with live device data.
            target_device = {"id": device_id, "kind": "hardware", "name": device_id}
        else:
            from gwprov.devices import device_rows
            target_device = next((row for row in device_rows() if row["id"] == device_id), None)
            if target_device is None:
                raise ValueError(f"device {device_id!r} is not currently available")
            if target_device.get("kind") != "hardware":
                raise ValueError("project --device currently requires a physical hardware target")
        assignment = get_assignment(device_id)
        profile_name = assignment.get("profile")
        assigned_card_name = assignment.get("sdcard")
        if not profile_name:
            raise ValueError(f"device {device_id} has no assigned profile; use `gwprov set profile PROFILE`")
    if args.start and target_device is None:
        raise ValueError("--start applies only to a direct flash project install with --device")
    if args.start and args.variant == "sd":
        raise ValueError("--start cannot be used with --variant sd")
    if profile_name:
        from gwprov.profiles import resolve_profile_path
        profile_root = resolve_profile_path(profile_name)
        variant_hint = _infer_project_variant_from_profile(profile_root)
        if args.dry_run:
            # install_project needs a destination for planning, but its dry-run
            # path never creates it. Avoid even a temporary filesystem change.
            output = str(profile_root / ".gwprov-project-preview")
        else:
            # Stage first so validation and downloads finish before changing the profile.
            import tempfile
            install_root = tempfile.TemporaryDirectory(prefix="gwprov-project-")
            output = str(Path(install_root.name) / "content")
    else:
        variant_hint = _infer_project_variant_from_directory(output)
    install_options = dict(
        output=output, variant=args.variant, versions_url=versions_url,
        variant_hint=variant_hint,
        target_id=args.target, tag=args.version, input_files=args.input,
        input_dirs=args.input_dir, firmware_files=args.firmware,
        firmware_dirs=args.firmware_dir, game_files=args.game,
        game_dirs=args.game_dir, bundle_path=bundle_path, dry_run=args.dry_run)
    if args.format == "json":
        from contextlib import redirect_stdout
        with redirect_stdout(sys.stderr):
            result = install_project(name, **install_options)
    else:
        result = install_project(name, **install_options)
    variant = result["variant"]
    if profile_root is not None and result.get("requiresAbi"):
        provision_path = profile_root / "provision.json"
        profile_metadata = json.loads(provision_path.read_text()) if provision_path.is_file() else {}
        firmware = profile_metadata.get("firmware", {})
        firmware = firmware.get("firmware", {}) if isinstance(firmware, dict) else {}
        available = firmware.get("providesAbi") if isinstance(firmware, dict) else None
        required = result["requiresAbi"]
        if (not isinstance(required, dict)
                or not isinstance(available, dict)
                or available.get("version") != required.get("version")
                or available.get("size", 0) < required.get("minSize", 0)):
            required_version = required.get("version", "unknown") if isinstance(required, dict) else "unknown"
            required_size = required.get("minSize", 0) if isinstance(required, dict) else 0
            raise ValueError(
                f"project requires Retro-Go ABI {required_version} "
                f"with at least {required_size} bytes; "
                "the profile's installed firmware ABI is missing or incompatible")
    if args.start and (target_device is None or variant != "flash"):
        raise ValueError("--start applies only to a direct flash project install with --device")
    if target_device is not None and variant == "sd":
        from gwprov.sd_cards import list_cards
        target_card = next((item for item in list_cards()
                            if item.get("name") == assigned_card_name), None) if assigned_card_name else None
        if target_card is None:
            raise ValueError(f"device {target_device['id']} has no registered SD card assignment; use `gwprov set sdcard CARD`")
    if args.dry_run and profile_root is not None and variant == "sd":
        from gwprov.profiles import DeviceProfile
        profile = DeviceProfile.load(profile_root)
        if profile.sd_mode != "bundled" or profile.resolved_sd is None:
            if install_root is not None:
                install_root.cleanup()
            raise ValueError("installing an SD project to a profile requires a bundled SD image")
    if args.dry_run:
        from pathlib import PurePosixPath

        planned = result.get("plannedFiles", [])
        if profile_root is not None:
            destination = {"kind": "profile", "profile": profile_name,
                           "path": str(profile_root.resolve())}
            if target_device is not None:
                destination["device"] = target_device["id"]
                destination["deviceName"] = target_device.get("name", target_device["id"])
            if target_card is not None:
                destination["sdCard"] = {"name": target_card["name"], "path": target_card["path"]}
            for item in planned:
                rel = PurePosixPath(item["path"])
                if variant == "sd":
                    item["destination"] = f"profile SD image/{rel}"
                    if target_card is not None:
                        item["deviceDestination"] = str(Path(target_card["path"]) / Path(*rel.parts))
                else:
                    partition = rel.parts[0] if rel.parts else "FrogFS"
                    label = "LittleFS" if partition == "littlefs" else "FrogFS"
                    item["destination"] = f"{label}/{PurePosixPath(*rel.parts[1:])}"
        else:
            output_root = Path(args.output).expanduser().resolve()
            destination = {"kind": "directory", "path": str(output_root)}
            for item in planned:
                item["destination"] = str(output_root / variant / item["path"])
        result["destination"] = destination
        result["plannedFiles"] = planned
        result["fileCount"] = len(planned)
        result["totalBytes"] = sum(item.get("bytes", 0) for item in planned)
        if result.get("mappedArtifacts"):
            payload_sizes = {item["path"].removeprefix("frogfs/"): item["bytes"]
                             for item in planned if item["path"].startswith("frogfs/")}
            frogfs_offset = 16 * 1024 * 1024
            if profile_root is not None:
                provision_path = profile_root / "provision.json"
                profile_metadata = json.loads(provision_path.read_text()) if provision_path.is_file() else {}
                layout = profile_metadata.get("layout", {})
                if isinstance(layout, dict) and layout.get("frogfsOffset") is not None:
                    frogfs_offset = int(layout["frogfsOffset"])
            for item in result["mappedArtifacts"]:
                base = item["relocBase"]
                if isinstance(base, str):
                    base = int(base, 0)
                item["relocBase"] = base
                if variant == "flash":
                    item["payloadBytes"] = payload_sizes.get(item["path"])
                    item["placement"] = "FrogFS, uncompressed"
                    item["relocation"] = {
                        "method": "for each aligned 32-bit word, add the runtime-minus-link-time delta when its Thumb-bit-cleared value points inside the linked payload; preserve the Thumb bit",
                        "pointerRange": f"[0x{base:08X}, 0x{base + item['payloadBytes']:08X}); Thumb bit ignored for range check and preserved in adjustment"
                        if item["payloadBytes"] is not None else None,
                        "runtimeAddressFormula": f"0x90000000 + 0x{frogfs_offset:X} + mapped payload offset",
                        "deltaFormula": f"runtime address - 0x{base:08X}",
                        "runtimeAddress": None,
                        "patchCount": None,
                    }
                else:
                    item["placement"] = "SD card; runtime relocation uses relocBase"
                    item["relocation"] = {
                        "method": "runtime relocation by the SD loader",
                        "runtimeAddressFormula": None,
                        "deltaFormula": None,
                        "patchCount": None,
                    }
        if target_device is not None:
            result["plannedOperation"] = (
                "update the assigned profile and deploy its Extflash regions to the device"
                if variant == "flash" else
                "update the assigned profile SD image and overlay files onto the registered SD card"
            )
        if install_root is not None:
            install_root.cleanup()
        if args.format == "json":
            _print_structured(result, "json")
        else:
            from rich import box
            from rich.table import Table
            from rich.text import Text

            console = Console()
            summary = Table(title="Project install plan", show_header=False, box=None, pad_edge=False)
            summary.add_column("Field", style="dim")
            summary.add_column("Value", overflow="fold")
            summary.add_row("Project", str(result.get('repo')))
            summary.add_row("Version", str(result.get('tag') or 'Local bundle'))
            summary.add_row("Target", str(result.get("target", "unknown")))
            summary.add_row("Variant", str(variant))
            summary.add_row("Destination", str(destination.get("path")))
            if result.get("plannedOperation"):
                summary.add_row("Device action", result["plannedOperation"])
            if destination.get("device"):
                summary.add_row("Device", str(destination.get("deviceName")))
            if destination.get("sdCard"):
                summary.add_row("SD card", str(destination['sdCard']['name']))
                summary.add_row("SD card path", str(destination['sdCard']['path']))
            summary.add_row("Files", str(len(planned)))
            summary.add_row("Total size", _human_size(result['totalBytes']))
            console.print(summary)
            if console.width < 100:
                from rich.console import Group

                entries = []
                for item in planned:
                    entry = Table.grid(padding=(0, 1), expand=True)
                    entry.add_column(style="dim", no_wrap=True)
                    entry.add_column(overflow="fold")
                    entry.add_row("File", Text(item["path"], style="bold"))
                    entry.add_row("Install to", Text(item["destination"]))
                    if target_card is not None:
                        entry.add_row("SD card path", Text(item.get("deviceDestination", "")))
                    entry.add_row("Size", _human_size(item["bytes"]))
                    entries.append(entry)
                console.print(Group(Text("Planned files", style="bold"), *entries))
            else:
                files = Table(title="Planned files", expand=True, show_lines=False)
                files.add_column("FILE", overflow="fold")
                files.add_column("INSTALLS TO", overflow="fold")
                if target_card is not None:
                    files.add_column("SD CARD PATH", overflow="fold")
                files.add_column("SIZE", justify="right", no_wrap=True)
                for item in planned:
                    row = [Text(item["path"]), Text(item["destination"])]
                    if target_card is not None:
                        row.append(Text(item.get("deviceDestination", "")))
                    row.append(_human_size(item["bytes"]))
                    files.add_row(*row)
                console.print(files)
            for mapped in result.get("mappedArtifacts", []):
                if variant == "flash":
                    console.print(
                        f"[cyan]Mapped XiP:[/] {mapped['path']} is relocated from "
                        f"0x{mapped['relocBase']:08X} to its FrogFS address in extflash.")
                else:
                    console.print(
                        f"[cyan]Mapped XiP:[/] {mapped['path']} is stored on the SD card "
                        "and relocated by the SD loader.")
            if result.get("bundle"):
                console.print("[dim]Local bundle. Publisher identity is not authenticated.[/dim]")
        return 0
    if profile_root is not None:
        import shutil
        from gwprov.filesystem import operate
        from gwprov.profiles import DeviceProfile

        staged = Path(output)
        profile = DeviceProfile.load(profile_root)
        if variant == "sd" and (profile.sd_mode != "bundled" or profile.resolved_sd is None):
            raise ValueError("installing an SD project to a profile requires a bundled SD image")
        staged_flash = staged / variant
        provision_path = profile_root / "provision.json"
        ownership_path = staged_flash / ".gwprov-projects.json"
        ownership = json.loads(ownership_path.read_text()) if ownership_path.is_file() else {}
        before = json.loads(provision_path.read_text()) if provision_path.is_file() else {}
        prior_projects_all = before.get("projects", {})
        new_identities = {
            (metadata.get("project"), metadata.get("target"))
            for metadata in ownership.values()
            if isinstance(metadata, dict) and metadata.get("project") and metadata.get("target")
        }
        replaced_projects = {
            key: metadata for key, metadata in prior_projects_all.items()
            if isinstance(metadata, dict)
            and (metadata.get("project"), metadata.get("target")) in new_identities
        }
        prior_projects = {
            key: metadata for key, metadata in prior_projects_all.items()
            if key not in replaced_projects
        }
        replacement_files = {
            path
            for metadata in replaced_projects.values()
            for path in metadata.get("files", [])
            if isinstance(path, str)
        }
        replacement_files.difference_update(
            path
            for metadata in ownership.values() if isinstance(metadata, dict)
            for path in metadata.get("files", [])
            if isinstance(path, str)
        )
        partitions = ((staged_flash / "frogfs", "frogfs"),
                      (staged_flash / "littlefs", "littlefs")) if variant == "flash" else ((staged / "sd", "fatfs"),)
        if target_device is not None:
            staged_files = [file_path for root, _ in partitions if root.is_dir()
                            for file_path in root.rglob("*") if file_path.is_file()]
            writes = [{"target": f"Profile {variant}: {file_path.relative_to(staged).as_posix()}",
                       "content": "Project file", "bytes": file_path.stat().st_size}
                      for file_path in sorted(staged_files)]
            _confirm_apply(args, bool(args.yes), [
                f"Project: {result.get('repo')}",
                f"Version: {result.get('tag')}",
                f"Profile: {profile_root.name}",
                f"Target: {target_device.get('name', target_device['id'])}",
                ("Action: update profile files and write its extflash regions"
                 if variant == "flash" else
                 "Action: update profile files and overlay its SD image onto the assigned card"),
            ], writes=writes)
        mapped_sources = {}
        if variant == "flash":
            staged_frogfs = staged_flash / "frogfs"
            staged_frogfs_files = any(staged_frogfs.rglob("*")) if staged_frogfs.is_dir() else False
            has_new_mapped = any(project.get("mapped") for project in ownership.values()
                                 if isinstance(project, dict))
            has_prior_mapped = any(project.get("mapped") for project in prior_projects.values()
                                   if isinstance(project, dict))
            if has_new_mapped or (has_prior_mapped and staged_frogfs_files):
                mapped_sources = _retain_profile_mapped_sources(
                    profile_root, staged_frogfs, ownership, prior_projects)
                provision = dict(before)
                provision["projects"] = dict(prior_projects)
                provision["projects"].update(ownership)
                provision_path.write_text(json.dumps(provision, indent=2) + "\n")
        # Remove stale files from a previous registration of the same project
        # before adding its replacement. This also lets bundle repo identities
        # replace an earlier local-project identity without duplicate XiP claims.
        for old_path in sorted(replacement_files):
            partition, separator, relative = old_path.partition("/")
            if not separator or not relative:
                continue
            filesystem = {"frogfs": "frogfs", "littlefs": "littlefs", "sd": "fatfs"}.get(partition)
            if filesystem is None or partition not in {name for _root, name in partitions}:
                continue
            target = "sd" if filesystem == "fatfs" else "flash/ext"
            sidecars = [f"{path}={source}" for path, source in mapped_sources.items()] if filesystem == "frogfs" else []
            operate(operation="delete", target=target, profile=str(profile_root),
                    filesystem=filesystem, path=relative, mapped_sources=sidecars)

        for root, filesystem in partitions:
            if not root.is_dir():
                continue
            files = sorted(p for p in root.rglob("*") if p.is_file())
            mapped_paths = set(mapped_sources) if filesystem == "frogfs" else set()
            seeded_mapped = False
            for file_path in files:
                rel = file_path.relative_to(root).as_posix()
                is_mapped_file = rel in mapped_paths
                if is_mapped_file and seeded_mapped:
                    # The first FrogFS rebuild seeds every mapped payload from
                    # its retained raw sidecar; do not add those files twice.
                    continue
                target = "flash/ext" if filesystem != "fatfs" else "sd"
                fs_option = filesystem if filesystem != "fatfs" else "fatfs"
                sidecars = [f"{path}={source}" for path, source in mapped_sources.items()
                            if not (is_mapped_file and path == rel)] if filesystem == "frogfs" else []
                operate(operation="add", target=target, profile=str(profile_root),
                        filesystem=fs_option, path=rel, source=str(file_path),
                        mapped_sources=sidecars)
                if filesystem == "frogfs" and mapped_paths:
                    seeded_mapped = True
        # Filesystem operations may update the FrogFS size in provision.json.
        # Merge project provenance after those operations so that layout update survives.
        provision = json.loads(provision_path.read_text()) if provision_path.is_file() else before
        project_records = provision.setdefault("projects", {})
        for key, metadata in ownership.items():
            project_records[key] = metadata
            for symbol in metadata.get("symbols", []):
                source = staged / "debug" / symbol
                if source.is_file():
                    destination = profile_root / "debug" / symbol
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, destination)
        provision_path.write_text(json.dumps(provision, indent=2) + "\n")
        if target_device is not None:
            if variant == "flash":
                from gwprov.deploy import apply_deployment, deployment_plan
                selected_regions = {"extflash", "frogfs", "littlefs"}
                plan = deployment_plan(str(profile_root))
                regions = [row["region"] for row in plan["regions"]
                           if row["region"] in selected_regions]
                if not regions:
                    raise ValueError("profile has no deployable extflash project regions")
                device_id = target_device["id"]
                remote_url = device_id.removeprefix("remote:") if device_id.startswith("remote:") else None
                progress, finish = _deployment_progress_display(args.format)
                try:
                    deployed = apply_deployment(
                        str(profile_root), probe_id=target_device.get("probeId"),
                        remote_url=remote_url,
                        remote_origin=target_device.get("remoteOrigin"),
                        regions=regions, start=args.start, bank=args.bank,
                        progress=progress)
                finally:
                    if finish:
                        finish()
                result["deployment"] = deployed
            else:
                from gwprov.deploy import overlay_sd_tree
                count = overlay_sd_tree(staged / "sd", Path(target_card["path"]))
                result["sdcard"] = {"name": target_card["name"], "path": target_card["path"],
                                    "filesWritten": count, "mode": "overlay"}
        install_root.cleanup()
        result["profile"] = str(profile_root)
        result["installed"] = True
    else:
        result["output"] = str(Path(args.output).expanduser().resolve())
    if target_device is not None:
        result["device"] = target_device["id"]
    if args.format == "json":
        _print_structured(result, "json")
    else:
        from rich.table import Table
        table = Table(title="Project install", show_header=False, box=None, pad_edge=False)
        table.add_column("Field", style="dim")
        table.add_column("Value", overflow="fold")
        table.add_row("Project", str(result.get('repo')))
        table.add_row("Version", str(result.get('tag') or 'Local bundle'))
        table.add_row("Target", str(result.get("target", "unknown")))
        table.add_row("Variant", str(result.get("variant", "unknown")))
        table.add_row("Files", str(len(result.get("files", []))))
        table.add_row("Installed to", str(result.get("profile") or args.output))
        if result.get("bundle"):
            table.add_row("Source", "Local bundle; publisher identity not authenticated")
        if result.get("deployment"):
            table.add_row("Device", str(target_device.get("name", result.get("device", "write complete"))))
        if result.get("sdcard"):
            table.add_row("SD card", str(result['sdcard']['name']))
            table.add_row("Files overlaid", str(result['sdcard']['filesWritten']))
        Console().print(table)
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
_gwprov_probe_names() {
  _gwprov_device_names | python3 -c 'import sys; print("\\n".join(x.strip() for x in sys.stdin if x.startswith("probe:")))'
}
_gwprov_retro_releases() {
  gwprov retro-go list --output json 2>/dev/null |
    python3 -c 'import json,sys; print("\\n".join(x["name"] for x in json.load(sys.stdin).get("releases", [])))' 2>/dev/null
}
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
    --device=*) COMPREPLY=( $(compgen -W "active $(_gwprov_device_names)" -- "${cur#*=}") ); return ;;
    --input=*|--input-dir=*|--firmware=*|--bios=*|--firmware-dir=*|--bios-dir=*|--game=*|--game-dir=*|--content=*|--source=*|--profile=*|--output-dir=*|--bootloader-file=*|--sd-bootloader-file=*|--backup-dir=*|--source-tree=*|--image=*|--shared-sd-root=*|--retro-go-root=*|--rom=*|--rom-dir=*|--config=*|--timeline=*|--record-timeline=*|--bank1=*|--bank2=*|--extflash=*|--sdcard=*|--gwemu-bin=*)
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
        projects:install|project:install|retro-go:config) format_values="text json" ;;
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
        apply:|deploy:plan|deploy:apply) output_values="text json yaml" ;;
        ps:*|projects:list|project:list|projects:ls|project:ls|projects:versions|project:versions|projects:info|project:info|devices|adapters:list|adapters:ls|gwemu:ps) output_values="text json" ;;
        profile:list|profile:ls|sdcard:list|sdcard:ls) output_values="text json names" ;;
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
  if [[ "$prev" == "--device" ]]; then
    candidates=$(_gwprov_device_names)
    COMPREPLY=( $(compgen -W "$candidates active" -- "$cur") )
    return
  fi
  if [[ "$prev" == "--probe-id" ]]; then
    candidates=$(_gwprov_probe_names)
    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
    return
  fi
  case "$prev:$context" in
    --format:tree:*) candidates="tree names" ;;
    --format:gwemu:profile) candidates="text json" ;;
    --format:profile:hardware|--format:perf:hardware) candidates="text json html pdf" ;;
    --format:report:render|--format:retro-go:config) candidates="text json" ;;
    --output:apply:|--output:deploy:plan|--output:deploy:apply|-o:apply:|-o:deploy:plan|-o:deploy:apply) candidates="text json yaml" ;;
    --output:ps:*|--output:projects:list|--output:project:list|--output:projects:ls|--output:project:ls|--output:projects:versions|--output:project:versions|--output:projects:info|--output:project:info|--output:gwemu:ps)
      candidates="text json" ;;
    --output:profile:list|--output:profile:ls|--output:sdcard:list|--output:sdcard:ls) candidates="text json names" ;;
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
    --bank:deploy:apply) candidates="1 2" ;;
    --bank:apply) candidates="1 2" ;;
    --bank:projects:install|--bank:project:install) candidates="1 2" ;;
    --oc-level:retro-go:config) candidates="0 1 2 3" ;;
    --filesystem:media:inventory) candidates="frogfs littlefs fatfs" ;;
    --mode:media:compare) candidates="contents image" ;;
    --extflash-mib:profile:create) candidates="64 128 256" ;;
    --model:profile:create) candidates="auto mario zelda" ;;
    *) candidates="" ;;
  esac
  if [[ -n "$candidates" && ( "$prev" == "--variant" || "$prev" == "--format" || "$prev" == "--output" || "$prev" == "-o" || "$prev" == "--region" || "$prev" == "--programmer" || "$prev" == "--target" || "$prev" == "--bank" || "$prev" == "--oc-level" || "$prev" == "--filesystem" || "$prev" == "--mode" || "$prev" == "--extflash-mib" || "$prev" == "--model" || "$prev" == "--timing-mode" || "$prev" == "--icount" ) ]]; then
    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
    return
  fi
  if [[ "$prev" == "-r" || "$prev" == "--retro-go" ]]; then
    candidates=$(_gwprov_retro_releases)
    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
    return
  fi
  if [[ "$prev" == "--input" || "$prev" == "--input-dir" || "$prev" == "--firmware" || "$prev" == "--bios" || "$prev" == "--firmware-dir" || "$prev" == "--bios-dir" || "$prev" == "--game" || "$prev" == "--game-dir" || "$prev" == "--content" || "$prev" == "--source" || "$prev" == "--profile" || "$prev" == "--output" || "$prev" == "--output-dir" || "$prev" == "--bootloader-file" || "$prev" == "--sd-bootloader-file" || "$prev" == "--backup-dir" || "$prev" == "--source-tree" || "$prev" == "--image" || "$prev" == "--shared-sd-root" || "$prev" == "--retro-go-root" || "$prev" == "--rom" || "$prev" == "--rom-dir" || "$prev" == "--config" || "$prev" == "--timeline" || "$prev" == "--record-timeline" || "$prev" == "--bank1" || "$prev" == "--bank2" || "$prev" == "--extflash" || "$prev" == "--sdcard" || "$prev" == "--symbols" || "$prev" == "--debug-config" || "$prev" == "--app-symbols" || "$prev" == "--gdb" || "$prev" == "--gwemu-bin" ]]; then
    _gwprov_complete_dirs "$cur"
    return
  fi
  if (( COMP_CWORD == 1 )) && [[ "$cur" != -* ]]; then
    candidates=$(gwprov tree --format names 2>/dev/null)
  elif (( COMP_CWORD == 2 )) && [[ "$cur" != -* ]]; then
    case "${COMP_WORDS[1]}" in
      show) candidates="adapters devices projects profile perf sdcard" ;;
      config) candidates="show" ;;
      set) candidates="active profile sdcard name" ;;
      deploy) candidates="plan apply" ;;
      ps) candidates="--help -h --output --no-pager --profile" ;;
      apply) candidates="--help -h -y --yes -f --force --start --bank -o --output" ;;
      completion) candidates="bash zsh" ;;
      adapters) candidates="list ls add remove rm" ;;
      device) candidates="recover" ;;
      projects|project) candidates="list ls versions info install stage-local" ;;
      backups) candidates="search list ls" ;;
      tree) candidates="--format --no-pager" ;;
      debug) candidates="python" ;;
      retro-go) candidates="list ls source install build config" ;;
      retro-go:source) candidates="add remove list ls default" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sdcard|sd) candidates="list ls add remove rm create compose" ;;
      input) candidates="tap" ;;
      report) candidates="render" ;;
      profile) candidates="create duplicate edit list ls show" ;;
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
      list|ls|versions|info) candidates=$(_gwprov_project_names) ;;
      install)
        if [[ "$cur" == */* || "$cur" == ~* || "$cur" == *.zip ]]; then
          _gwprov_complete_dirs "$cur"
          return
        fi
        candidates=$(_gwprov_project_names)
        ;;
      stage-local)
        COMPREPLY=()
        while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(compgen -f -- "$cur")
        return
        ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == debug && "${COMP_WORDS[2]-}" == python && "$cur" != -* ]]; then
    candidates=$(_gwprov_device_names)
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == retro-go && "${COMP_WORDS[2]-}" == config && "$cur" != -* ]]; then
    candidates=$(_gwprov_device_names)
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == profile && "${COMP_WORDS[2]-}" == edit && "$cur" != -* ]]; then
    candidates="$(_gwprov_profile_names)"
  elif (( COMP_CWORD == 4 )) && [[ "${COMP_WORDS[1]-}" == profile && "${COMP_WORDS[2]-}" == edit && "$cur" != -* ]]; then
    candidates="add remove"
  elif (( COMP_CWORD == 5 )) && [[ "${COMP_WORDS[1]-}" == profile && "${COMP_WORDS[2]-}" == edit && "${COMP_WORDS[4]-}" == add && "$cur" != -* ]]; then
    candidates="bank1 bank2 bank0 extflash"
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
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == deploy && ( "${COMP_WORDS[2]-}" == plan || "${COMP_WORDS[2]-}" == apply ) && "$cur" != -* ]]; then
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
      deploy:plan) candidates="--region -o --output" ;;
      deploy:apply) candidates="--probe-id --programmer --remote-url --remote-origin --region --start --bank -o --output" ;;
      projects:list|project:list|projects:ls|project:ls) candidates="--firmware-repo --output" ;;
      projects:versions|project:versions) candidates="--output" ;;
      projects:info|project:info) candidates="--version --output" ;;
      projects:install|project:install) candidates="--version --target --variant --output --profile --device --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run --format --yes --start --bank" ;;
      projects:stage-local|project:stage-local) candidates="--output" ;;
      adapters:list|adapters:ls) candidates="--output" ;;
      adapters:add) candidates="--origin" ;;
      tree:*) candidates="--format --no-pager" ;;
      retro-go:install) candidates="--repo --version --variant --output" ;;
      retro-go:build) candidates="--path --make-target --bank --extflash-part-mb --extflash-offset-mb --jobs --makevar --clean --docker --quiet --dry-run" ;;
      retro-go:config) candidates="--profile --output --format --auto-start --no-auto-start --rom --timeout --menu-timeout --oc-level --selected-tab --cursor --browse-subpath --yes --force" ;;
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
      filesystem:ls|filesystem:tree|filesystem:add|filesystem:delete|filesystem:del|filesystem:remove|filesystem:rm|fs:ls|fs:tree|fs:add|fs:delete|fs:del|fs:remove|fs:rm) candidates="--profile --image --target --filesystem --offset --size --block-size --source --mapped-source" ;;
      input:tap) candidates="--probe-id --programmer --remote-url --remote-origin --repeat --tap-ms --gap-ms" ;;
      media:compare) candidates="--mode" ;;
      media:frogfs|media:littlefs) candidates="--retro-go-root" ;;
      sdcard:create|sd:create) candidates="--size-mb --label" ;;
      sdcard:compose|sd:compose) candidates="--content-dir --core --core-name --rom --rom-dir --config" ;;
      input:tap) candidates="--repeat --tap-ms --gap-ms" ;;
      report:render) candidates="--input --output --format" ;;
      profile:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      perf:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      profile:create) candidates="-m --mario -z --zelda -p --patched -b --bootloader -r --retro-go --content --output-dir --name --littlefs --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file --sd-bootloader-file" ;;
      profile:duplicate) candidates="--output-dir --output" ;;
      profile:list|profile:ls) candidates="--output --no-pager" ;;
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
_gwprov_probe_names() {
  print -r -- "${(M)${(@f)$(_gwprov_device_names)}:#probe:*}"
}
_gwprov_retro_releases() {
  gwprov retro-go list --output json 2>/dev/null |
    python3 -c 'import json,sys; print("\n".join(x["name"] for x in json.load(sys.stdin).get("releases", [])))' 2>/dev/null
}
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
    --input=*|--input-dir=*|--firmware=*|--bios=*|--firmware-dir=*|--bios-dir=*|--game=*|--game-dir=*|--content=*|--source=*|--profile=*|--output-dir=*|--bootloader-file=*|--sd-bootloader-file=*|--backup-dir=*|--source-tree=*|--image=*|--shared-sd-root=*|--retro-go-root=*|--rom=*|--rom-dir=*|--config=*|--timeline=*|--record-timeline=*|--bank1=*|--bank2=*|--extflash=*|--sdcard=*|--symbols=*|--debug-config=*|--app-symbols=*|--gdb=*|--gwemu-bin=*)
      prefix=${cur%%=*}=; pathpart=${cur#*=}
      IPREFIX=$prefix PREFIX=$pathpart _files
      return
      ;;
    --variant=*) IPREFIX=--variant= PREFIX=${cur#*=}; compadd -- flash sd; return ;;
    --format=*)
      case "$context" in
        tree:*) compadd -- "${cur%%=*}=tree" "${cur%%=*}=names" ;;
        gwemu:profile) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" ;;
        projects:install|project:install|retro-go:config) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" ;;
        profile:hardware|perf:hardware) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" "${cur%%=*}=html" "${cur%%=*}=pdf" ;;
        report:render) compadd -- "${cur%%=*}=html" "${cur%%=*}=pdf" ;;
      esac
      return
      ;;
    --output=*)
      case "$context" in
        profile:list|profile:ls|sdcard:list|sdcard:ls) compadd -- "${cur%%=*}=text" "${cur%%=*}=json" "${cur%%=*}=names"; return ;;
        profile:duplicate) compadd -- "${cur%%=*}=text" "${cur%%=*}=json"; return ;;
        apply:|deploy:plan|deploy:apply)
          compadd -- "${cur%%=*}=text" "${cur%%=*}=json" "${cur%%=*}=yaml"; return ;;
        ps:*|projects:list|project:list|projects:ls|project:ls|projects:versions|project:versions|projects:info|project:info|devices|adapters:list|adapters:ls|gwemu:ps)
          compadd -- "${cur%%=*}=text" "${cur%%=*}=json"; return ;;
      esac
      prefix=${cur%%=*}=; pathpart=${cur#*=}; IPREFIX=$prefix PREFIX=$pathpart _files; return
      ;;
  esac
  case "$prev" in
    --profile) _gwprov_profile_complete "$cur"; return ;;
    --device) compadd -- active "${(@f)$(_gwprov_device_names)}"; return ;;
    --probe-id) compadd -- "${(@f)$(_gwprov_probe_names)}"; return ;;
    -r|--retro-go) compadd -- "${(@f)$(_gwprov_retro_releases)}"; return ;;
    --input|--input-dir|--firmware|--bios|--firmware-dir|--bios-dir|--game|--game-dir|--content|--source|--profile|--output|-o|--output-dir|--bootloader-file|--sd-bootloader-file|--backup-dir|--source-tree|--image|--shared-sd-root|--retro-go-root|--rom|--rom-dir|--config|--timeline|--record-timeline|--bank1|--bank2|--extflash|--sdcard|--symbols|--debug-config|--app-symbols|--gdb|--gwemu-bin)
      _files; return ;;
    --variant) compadd -- flash sd; return ;;
  esac
  case "$prev:$context" in
    --format:tree:*) compadd -- tree names; return ;;
    --format:gwemu:profile) compadd -- text json; return ;;
    --format:profile:hardware|--format:perf:hardware) compadd -- text json html pdf; return ;;
    --format:report:render) compadd -- html pdf; return ;;
    --format:retro-go:config) compadd -- text json; return ;;
    --output:profile:list|--output:profile:ls|--output:sdcard:list|--output:sdcard:ls) compadd -- text json names; return ;;
    --output:profile:duplicate) compadd -- text json; return ;;
    --output:apply:|--output:deploy:plan|--output:deploy:apply|-o:apply:|-o:deploy:plan|-o:deploy:apply) compadd -- text json yaml; return ;;
    --output:ps:*|--output:projects:list|--output:project:list|--output:projects:ls|--output:project:ls|--output:projects:versions|--output:project:versions|--output:projects:info|--output:project:info|--output:gwemu:ps) compadd -- text json; return ;;
    --region:deploy:plan|--region:deploy:apply) compadd -- bank1 bank2 frogfs littlefs extflash sd; return ;;
    --programmer:deploy:apply|--programmer:debug:python|--programmer:profile:hardware|--programmer:perf:hardware) compadd -- stlink jlink cmsis-dap rpi-gpio; return ;;
    --target:debug:python) compadd -- gwemu hardware; return ;;
    --target:filesystem:*|--target:fs:*) compadd -- flash/ext sdcard sd; return ;;
    --filesystem:filesystem:create|--filesystem:fs:create) compadd -- frogfs littlefs lfs fatfs sdcard sd; return ;;
    --icount:gwemu:start|--icount:gwemu:run) compadd -- 0 1 2 3 4 5 6 7 8 9 10; return ;;
    --timing-mode:gwemu:start|--timing-mode:gwemu:run) compadd -- default baseline experimental-m7; return ;;
    --bank:retro-go:build|--bank:gwemu:run) compadd -- 1 2; return ;;
    --bank:deploy:apply) compadd -- 1 2; return ;;
    --bank:apply) compadd -- 1 2; return ;;
    --bank:projects:install|--bank:project:install) compadd -- 1 2; return ;;
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
      adapters) candidates="list ls add remove rm" ;;
      device) candidates="recover" ;;
      projects|project) candidates="list ls versions info install stage-local" ;;
      backups) candidates="search list ls" ;;
      tree) candidates="--format --no-pager" ;;
      debug) candidates="python" ;;
      retro-go) candidates="list ls source install build config" ;;
      retro-go:source) candidates="add remove list ls default" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sdcard|sd) candidates="list ls add remove rm create compose" ;;
      input) candidates="tap" ;;
      report) candidates="render" ;;
      profile) candidates="create duplicate edit list ls show" ;;
      perf) candidates="hardware" ;;
      filesystem|fs) candidates="create ls tree add delete del remove rm" ;;
      show) candidates="adapters devices projects profile perf sdcard" ;;
        config) candidates="show" ;;
      set) candidates="active profile sdcard name" ;;
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
      list|ls|versions|info) candidates="$( _gwprov_project_names )" ;;
      install)
        if [[ "$cur" == */* || "$cur" == ~* || "$cur" == *.zip ]]; then
          _files -g '*.zip'
          return
        fi
        candidates="$( _gwprov_project_names )"
        ;;
      stage-local) _files; return ;;
      *) candidates="" ;;
    esac
  elif (( CURRENT == 4 )) && [[ ${words[2]} == debug && ${words[3]} == python && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_device_names)}"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == retro-go && ${words[3]} == config && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_device_names)}"
  elif (( CURRENT == 4 )) && [[ ${words[2]} == profile && ${words[3]} == edit && "$cur" != -* ]]; then
    candidates="${(@f)$(_gwprov_profile_names)}"
  elif (( CURRENT == 5 )) && [[ ${words[2]} == profile && ${words[3]} == edit && "$cur" != -* ]]; then
    candidates="add remove"
  elif (( CURRENT == 6 )) && [[ ${words[2]} == profile && ${words[3]} == edit && ${words[5]} == add && "$cur" != -* ]]; then
    candidates="bank1 bank2 bank0 extflash"
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
  elif (( CURRENT == 4 )) && [[ ${words[2]} == deploy && ( ${words[3]} == plan || ${words[3]} == apply ) && "$cur" != -* ]]; then
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
      deploy:plan) candidates="--region -o --output" ;;
      deploy:apply) candidates="--probe-id --programmer --remote-url --remote-origin --region --start --bank -o --output" ;;
      apply) candidates="--help -h -y --yes -f --force --start --bank -o --output" ;;
      projects:list|project:list|projects:ls|project:ls) candidates="--firmware-repo --output" ;;
      projects:versions|project:versions) candidates="--output" ;;
      projects:info|project:info) candidates="--version --output" ;;
      projects:install|project:install) candidates="--version --target --variant --output --profile --device --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run --format --yes --start --bank" ;;
      projects:stage-local|project:stage-local) candidates="--output" ;;
      adapters:list|adapters:ls) candidates="--output" ;;
      adapters:add) candidates="--origin" ;;
      tree:*) candidates="--format --no-pager" ;;
      retro-go:install) candidates="--repo --version --variant --output" ;;
      retro-go:build) candidates="--path --make-target --bank --extflash-part-mb --extflash-offset-mb --jobs --makevar --clean --docker --quiet --dry-run" ;;
      retro-go:config) candidates="--profile --output --format --auto-start --no-auto-start --rom --timeout --menu-timeout --oc-level --selected-tab --cursor --browse-subpath --yes --force" ;;
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
      filesystem:ls|filesystem:tree|filesystem:add|filesystem:delete|filesystem:del|filesystem:remove|filesystem:rm|fs:ls|fs:tree|fs:add|fs:delete|fs:del|fs:remove|fs:rm) candidates="--profile --image --target --filesystem --offset --size --block-size --source --mapped-source" ;;
      input:tap) candidates="--probe-id --programmer --remote-url --remote-origin --repeat --tap-ms --gap-ms" ;;
      media:compare) candidates="--mode" ;;
      media:frogfs|media:littlefs) candidates="--retro-go-root" ;;
      sdcard:create|sd:create) candidates="--size-mb --label" ;;
      sdcard:compose|sd:compose) candidates="--content-dir --core --core-name --rom --rom-dir --config" ;;
      input:tap) candidates="--repeat --tap-ms --gap-ms" ;;
      report:render) candidates="--input --output --format" ;;
      profile:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      perf:hardware) candidates="--probe-id --programmer --remote-url --remote-origin --profile --symbols --counter-symbol --frame-counter --total-cycle-counter --start-at --set-register --startup-timeout --duration --interval --output --format --top" ;;
      profile:create) candidates="-m --mario -z --zelda -p --patched -b --bootloader -r --retro-go --content --output-dir --name --littlefs --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file --sd-bootloader-file" ;;
      profile:duplicate) candidates="--output-dir --output" ;;
      profile:list|profile:ls) candidates="--output --no-pager" ;;
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
        ("Help and shell integration", ("tree", "completion", "daemon")),
    )
    commands = parser.add_subparsers(dest="command", required=True,
                                     metavar="COMMAND",
                                     parser_class=GWProvArgumentParser)
    daemon = commands.add_parser("daemon", help="inspect and restart the per-user service")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_restart = daemon_commands.add_parser(
        "restart", help="drain and restart the daemon")
    daemon_restart.add_argument("--force", action="store_true",
                                help="cancel active substantial device operations")
    daemon_restart.add_argument("--timeout", type=float, default=30.0,
                                help="seconds to wait for the restart (default: 30)")
    daemon_restart.set_defaults(handler=_daemon_restart)
    show = commands.add_parser("show", help="overview of GWProv resources")
    show.add_argument("section", nargs="?", choices=("adapters", "devices", "projects", "profile", "perf", "sdcard"))
    show.add_argument("--output", choices=("text", "json"), default="text")
    show.add_argument("--firmware-repo", default="sylverb/game-and-watch-retro-go-sd",
                      help=argparse.SUPPRESS)
    show.set_defaults(handler=_show)
    adapters = commands.add_parser("adapters", help="list and manage hardware adapters")
    adapters.set_defaults(handler=_adapters_list, output="text")
    adapter_commands = adapters.add_subparsers(dest="adapter_command")
    adapter_list = adapter_commands.add_parser("list", aliases=("ls",), help="list local and registered adapters")
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
        "recover", help="start or synchronize with the gnwmanager RAM flasher")
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
                             ("sdcard", "assign a registered SD card to the active device"),
                             ("name", "set a display name for the active device")):
        assignment = set_commands.add_parser(field, help=help_text)
        assignment.add_argument("value", nargs="?", metavar="VALUE",
                                help="new value; use clear to remove the assignment")
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
    apply = commands.add_parser(
        "apply", help="apply the assigned profile to the active device",
        description="Apply the assigned profile to the active device.",
        epilog="Examples:\n  gwprov apply\n  gwprov apply --yes\n  gwprov apply -o json --force\n  gwprov apply -o yaml --force")
    apply.add_argument("-y", "--yes", "-f", "--force", dest="yes", action="store_true",
                       help="skip the confirmation prompt")
    apply.add_argument("--start", action="store_true",
                       help="start firmware after writing; otherwise leave the RAM flasher running")
    apply.add_argument("--bank", type=int, choices=(1, 2), default=1,
                       help="bank to start with --start (default: 1)")
    apply.add_argument("-o", "--output", choices=("text", "json", "yaml"), default="text",
                       help="show progress, or emit a JSON/YAML result")
    apply.set_defaults(handler=_apply_parser)
    ps = commands.add_parser("ps", help="show live GWemu and hardware state")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    ps.add_argument("--profile", help="load firmware/app symbols for physical Application state")
    ps.add_argument("--live", "--refresh", dest="refresh", action="store_true",
                    help="force a live runtime and internal-flash scan")
    ps.set_defaults(handler=_ps)
    deploy = commands.add_parser("deploy", help="plan and apply firmware or filesystem writes")
    deploy_commands = deploy.add_subparsers(dest="deploy_command", required=True)
    deploy_plan = deploy_commands.add_parser("plan", help="show exact bank and filesystem writes",
                                             description="Show the profile blobs, destination ranges, sizes, and hashes without connecting to hardware.",
                                             epilog="Examples:\n  gwprov deploy plan basic-test\n  gwprov deploy plan basic-test -o yaml")
    deploy_plan.add_argument("profile", metavar="PROFILE",
                             help="profile name or path to plan")
    deploy_plan.add_argument("--region", action="append", choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"))
    deploy_plan.add_argument("-o", "--output", choices=("text", "json", "yaml"), default="text")
    deploy_plan.set_defaults(handler=_deploy_plan)
    deploy_apply = deploy_commands.add_parser("apply", help="write selected profile regions to hardware")
    deploy_apply.add_argument("profile", metavar="PROFILE",
                              help="profile name or path to deploy")
    deploy_target = deploy_apply.add_mutually_exclusive_group()
    deploy_target.add_argument("--probe-id", help="select a local PyOCD probe by unique ID")
    deploy_target.add_argument("--programmer", choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                              help="select one local OpenOCD adapter explicitly")
    deploy_target.add_argument("--remote-url", help="use one gnwmanager serve URL")
    deploy_apply.add_argument("--remote-origin", help="Origin required by the selected remote server")
    deploy_apply.add_argument("-o", "--output", choices=("text", "json", "yaml"), default="text",
                              help="show progress, or emit a JSON/YAML result")
    deploy_apply.add_argument("--region", action="append", choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"),
                              help="region to deploy; repeatable, defaults to all profile regions")
    deploy_apply.add_argument("--start", action="store_true",
                              help="start firmware after writing; otherwise leave the RAM flasher running")
    deploy_apply.add_argument("--bank", type=int, choices=(1, 2), default=1,
                              help="bank to start with --start (default: 1)")
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
        "python", help="attach a persistent Python console to a selected device",
        description="Attach to a named or active device. Hardware autostart can be skipped with --attach-only.",
        epilog="Examples:\n  gwprov debug python\n  gwprov debug python --attach-only\n  gwprov debug python --attach-only --no-watchdog-freeze\n  gwprov debug python --autostart-timeout 20\n  gwprov debug python probe:290077000E0000513751594E\n  gwprov debug python 'GWemu pid 12345'\n  gwprov debug python --target hardware --probe-id 290077000E0000513751594E")
    python_debug.add_argument("device", nargs="?", help="device name or ID; defaults to the active device")
    python_debug.add_argument("--target", choices=("gwemu", "hardware"),
                              help="target kind when using an explicit backend or the active device")
    python_debug.add_argument("--host", default="127.0.0.1")
    python_debug.add_argument("--port", type=int,
                              help="GWemu GDB port; inferred for a managed instance")
    python_debug.add_argument("--openocd-port", type=int, default=6666)
    python_debug.add_argument("--probe-id", action="append", default=[],
                              help="select a local PyOCD probe by unique ID (repeatable; defaults to the active device)")
    python_debug.add_argument("--programmer", action="append", default=[],
                              choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                              help="start an explicit OpenOCD adapter session (repeatable by type)")
    python_debug.add_argument("--remote-url", action="append", default=[],
                              help="connect through the daemon to one gnwmanager serve URL; repeat for independent servers")
    python_debug.add_argument("--remote-origin", action="append", default=[],
                              help="Origin header for the corresponding remote URL (repeatable)")
    python_debug.add_argument("--qmp-socket", help="GWProv-managed QMP handle; normally discovered from the selected device")
    python_debug.add_argument("--debug-config", help="local ELF/map, relocation and counter descriptor")
    python_debug.add_argument("--profile", help="select a symbol profile when it is not inferred from the selected device")
    python_debug.add_argument("--symbols", action="append", default=[], metavar="ELF",
                              help="load additional firmware, app, or project ELF symbols (repeatable)")
    python_debug.add_argument("--paused", action="store_true",
                              help="set start_paused at the project's app_main autostart hook")
    python_debug.add_argument("--attach-only", action="store_true",
                              help="open the Python console without resetting or waiting for app_main")
    python_debug.add_argument("--autostart-timeout", type=float, metavar="SECONDS",
                              help="return to the Python console if automatic app_main launch takes this long")
    python_debug.add_argument("--no-watchdog-freeze", action="store_true",
                              help="do not write MCU watchdog-freeze bits on attach")
    python_debug.set_defaults(handler=_debug_python)

    project = commands.add_parser("projects", aliases=("project",),
                                  help="inspect and stage GWRG-distributed projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser("list", aliases=("ls",), help="list curated cores and homebrew projects")
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
    install = project_commands.add_parser(
        "install", help="verify a release or bundle ZIP and install it into content, a profile, or a device",
        description=("Install a GWRG project from the network or an offline ZIP.\n"
                     "Variant is inferred from the destination or project release.\n"
                     "ZIP hashes verify integrity, not publisher identity.\n"
                     "See docs/PROFILES.md for the bundle format and examples.\n\n"
                     "Examples:\n"
                     "  gwprov project install doom --profile demo\n"
                     "  gwprov project install ./doom.zip --device --yes"))
    install.add_argument("repo", help="curated project, owner/repo, or path to a GWRG bundle ZIP")
    install.add_argument("--version", help="published tag; defaults to newest")
    install.add_argument("--target", help="target id when the manifest publishes more than one")
    install.add_argument("--variant", choices=("flash", "sd"),
                         help="override inferred destination/release variant")
    install_target = install.add_mutually_exclusive_group(required=True)
    install_target.add_argument("--output", help="staging root directory")
    install_target.add_argument("--profile", help="install into a managed profile for a later apply")
    install_target.add_argument("--device", nargs="?", const="active", metavar="DEVICE",
                                help="install to a physical device with an assigned profile; defaults to the active device")
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
    install.add_argument("--dry-run", action="store_true",
                         help="show detected variant, files and destinations without writing or contacting a device")
    install.add_argument("--format", choices=("text", "json"), default="text",
                         help="result format (default: text)")
    install.add_argument("-y", "--yes", action="store_true",
                         help="skip confirmation when installing directly to a device")
    install.add_argument("--start", action="store_true",
                         help="start the selected bank after a flash project install")
    install.add_argument("--bank", type=int, choices=(1, 2), default=1,
                         help="bank to start with --start (default: 1)")
    install.set_defaults(handler=_project_install)
    stage_local = project_commands.add_parser("stage-local", help="stage an unpublished local build into content")
    stage_local.add_argument("manifest", help="local JSON manifest describing sources and destinations")
    stage_local.add_argument("--output", required=True, help="content root populated by retro-go install")
    stage_local.set_defaults(handler=_project_stage_local)

    backups = commands.add_parser("backups", help="discover and list verified stock firmware backups")
    backup_commands = backups.add_subparsers(dest="backup_action", required=True)
    backup_search = backup_commands.add_parser("search", help="recursively find stock backup pairs")
    backup_search.add_argument("directory", nargs="?", default=".", help="search root (default: current directory)")
    backup_search.add_argument("--output", choices=("text", "json"), default="text")
    backup_search.set_defaults(handler=_backups)
    backup_list = backup_commands.add_parser("list", aliases=("ls",), help="list discovered backup pairs")
    backup_list.add_argument("--output", choices=("text", "json"), default="text")
    backup_list.set_defaults(handler=_backups)

    retro_go = commands.add_parser("retro-go", help="manage Retro-Go releases and file workflows")
    retro_commands = retro_go.add_subparsers(dest="retro_command", required=True)
    release_list = retro_commands.add_parser("list", aliases=("ls",), help="list available releases from registered sources")
    release_list.add_argument("source", nargs="?", help="limit to one registered source")
    release_list.add_argument("--sort", choices=("source", "version"), default="version")
    release_list.add_argument("--reverse", action="store_true")
    release_list.add_argument("--output", choices=("text", "json"), default="text")
    release_list.set_defaults(handler=_retro_release_list)
    retro_source = retro_commands.add_parser("source", help="manage release index sources")
    source_commands = retro_source.add_subparsers(dest="source_action", required=True)
    source_add = source_commands.add_parser("add", help="register a GitHub repository or versions.json URL")
    source_add.add_argument("source")
    source_add.set_defaults(handler=_retro_sources)
    source_remove = source_commands.add_parser("remove", help="remove a registered source")
    source_remove.add_argument("source")
    source_remove.set_defaults(handler=_retro_sources)
    source_list = source_commands.add_parser("list", aliases=("ls",), help="list registered sources")
    source_list.add_argument("--output", choices=("text", "json"), default="text")
    source_list.set_defaults(handler=_retro_sources)
    source_default = source_commands.add_parser("default", help="show or select the default source")
    source_default.add_argument("source", nargs="?")
    source_default.set_defaults(handler=_retro_sources)
    firmware_install = retro_commands.add_parser("install", help="download verified release firmware and content")
    firmware_install.add_argument("--repo", default="sylverb/game-and-watch-retro-go-sd")
    firmware_install.add_argument("--versions-url", help=argparse.SUPPRESS)
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

    config = retro_commands.add_parser(
        "config", help="show or change Retro-Go /CONFIG for a device or profile",
        description=("Read Retro-Go's /CONFIG, or change selected settings inline.\n\n"
                     "Examples:\n"
                     "  gwprov retro-go config retro-go-zelda\n"
                     "  gwprov retro-go config retro-go-zelda --auto-start /homebrews/demo.bin --timeout 0\n"
                     "  gwprov retro-go config --profile basic-test --oc-level 2"))
    config.add_argument("device", nargs="?", metavar="DEVICE",
                        help="device name or ID; required unless --profile is used")
    config.add_argument("--profile", metavar="PROFILE",
                        help="read or update a managed profile instead of a device")
    config.add_argument("--output", metavar="FILE",
                        help="also save the resulting 416-byte CONFIG blob")
    config.add_argument("--format", choices=("text", "json"), default="text",
                        help="display format (default: text)")
    auto_start = config.add_mutually_exclusive_group()
    auto_start.add_argument("--auto-start", metavar="FILE_PATH",
                            help="launch this Retro-Go file at boot")
    auto_start.add_argument("--no-auto-start", action="store_true",
                            help="clear the automatic launch file and show the launcher")
    config.add_argument("--rom", nargs=2, metavar=("CORE", "FILENAME"),
                        help="set auto-start from a core and filename, for example --rom gb Tetris.gb")
    config.add_argument("--timeout", "--menu-timeout", dest="timeout", type=int,
                        help="main-menu idle timeout in seconds; use 0 to disable")
    config.add_argument("--oc-level", type=int, choices=range(4))
    config.add_argument("--selected-tab", type=int)
    config.add_argument("--cursor", type=int)
    config.add_argument("--browse-subpath")
    config.add_argument("-y", "--yes", "-f", "--force", action="store_true",
                        help="skip confirmation before writing a device")
    config.set_defaults(handler=_retro_go_config)

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
    debug.add_argument("--gdb-port", type=int,
                       help="GDB port; automatic allocation starts at 1235")
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
    patch.add_argument("--backup-dir", required=True, help="directory of stock internal and external backups")
    patch.add_argument("--output-dir", required=True)
    patch.add_argument("--extflash-mib", type=int, choices=(64, 128, 256), default=64)
    patch.add_argument("--no-bootloader", action="store_true")
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
    add_fs.add_argument("--mapped-source", action="append", default=[], metavar="PATH=FILE",
                        help="raw source for a mapped XiP file when rebuilding FrogFS; repeat for each mapped path")
    delete_fs = filesystem_commands.add_parser("delete", aliases=("del", "remove", "rm"), help="remove a file")
    filesystem_target(delete_fs)
    delete_fs.add_argument("path", help="file path inside the filesystem")
    delete_fs.add_argument("--mapped-source", action="append", default=[], metavar="PATH=FILE",
                           help="raw source for every retained mapped XiP file when rebuilding FrogFS")

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
                                              metavar="{create,duplicate,edit,list,ls,show}")
    create = profile_commands.add_parser("create", help="create a device profile from content or stock media")
    create.add_argument("directory", metavar="NAME_OR_PATH",
                        help="managed profile name, or an explicit output path")
    create.add_argument("--output-dir",
                        help="profile store for this creation (default: GWPROV_PROFILE_DIR or XDG data home)")
    model_group = create.add_mutually_exclusive_group()
    model_group.add_argument("-m", "--mario", action="store_true", help="include Mario stock firmware and assets")
    model_group.add_argument("-z", "--zelda", action="store_true", help="include Zelda stock firmware and assets")
    create.add_argument("-p", "--patched", action="store_true", help="apply the standard stock firmware patch")
    create.add_argument("-b", "--bootloader", action="store_true",
                        help="include the SD bootloader used by Game+Left at 0x08032000")
    create.add_argument("--content", help="staged content root populated by Retro-Go/project install")
    create.add_argument("-r", "--retro-go", metavar="SOURCE:VERSION",
                        help="add an exact release listed by `gwprov retro-go list`")
    create.add_argument("--name")
    create.add_argument("--littlefs", "--littlefs-mib", dest="littlefs_mib", type=int, default=2, metavar="SIZE_MB")
    create.add_argument("--sd-size-mib", type=int, default=128,
                        help="capacity of the bundled FAT32 image for SD firmware (default: 128)")
    create.add_argument("--sd-label", default="RETROGO",
                        help="volume label for the bundled SD image")
    create.add_argument("--extflash-mib", type=int, choices=(64, 128, 256),
                        help="chip capacity; flash defaults to the smallest size that fits, SD defaults to 64 MiB")
    create.add_argument("--bootloader-repo", default="sylverb/game-and-watch-bootloader")
    create.add_argument("--bootloader-version", default="v1.0.8", help="release tag or latest")
    create.add_argument("--bootloader-file", help="use a local binary linked at 0x08000000")
    create.add_argument("--sd-bootloader-file",
                        help="use a local SD bootloader linked at 0x08032000 (requires -b)")
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
    edit = profile_commands.add_parser("edit", help="change a profile's mapped flash components")
    edit.add_argument("profile", metavar="PROFILE", help="managed profile name or profile path")
    edit_commands = edit.add_subparsers(dest="action", required=True, metavar="{add,remove}")
    blob_add = edit_commands.add_parser("add", help="add or replace one component")
    blob_add.add_argument("bank", choices=("bank1", "bank2", "bank0", "extflash"),
                          help="internal bank1/bank2 or external flash bank0/extflash")
    blob_add.add_argument("name", help="display name used in apply summaries")
    blob_add.add_argument("file", help="source blob file")
    blob_add.add_argument("--offset", required=True, type=lambda value: int(value, 0),
                          help="offset within the selected bank (decimal or 0x-prefixed)")
    blob_add.add_argument("--replace", action="store_true",
                          help="replace an existing blob with the same name in that bank")
    blob_add.add_argument("-o", "--output", choices=("text", "json"), default="text")
    blob_add.set_defaults(handler=_profile_blob, action="add")
    blob_remove = edit_commands.add_parser("remove", help="remove a mapped component")
    blob_remove.add_argument("bank", choices=("bank1", "bank2", "bank0", "extflash"))
    blob_remove.add_argument("name", help="display name of the component to remove")
    blob_remove.add_argument("-o", "--output", choices=("text", "json"), default="text")
    blob_remove.set_defaults(handler=_profile_blob, action="remove")
    profile_list = profile_commands.add_parser("list", aliases=("ls",), help="list profiles in the managed profile store")
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
