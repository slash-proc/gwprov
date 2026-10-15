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
                    action._choices_actions = [
                        choice for choice in original if choice.help != argparse.SUPPRESS
                    ]
                    hidden_choices.append((action, original))
            try:
                return super().format_help()
            finally:
                for action, original in hidden_choices:
                    action._choices_actions = original

        formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        action_groups = sorted(
            self._action_groups, key=lambda group: 0 if group.title.lower() == "options" else 1
        )
        for action_group in action_groups:
            subparsers = [
                action
                for action in action_group._group_actions
                if isinstance(action, argparse._SubParsersAction)
            ]
            ordinary = [
                action
                for action in action_group._group_actions
                if not isinstance(action, argparse._SubParsersAction)
            ]
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
                remaining = [
                    choice for choice in action._choices_actions if choice.dest not in shown
                ]
                if remaining:
                    formatter.start_section("Other commands")
                    formatter.add_arguments(remaining)
                    formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _build_retro_go(args) -> int:
    from gwprov.common.retrogo_build import build, stage_build

    variant = args.variant
    if args.profile:
        from gwprov.profiles import DeviceProfile

        profile_name = args.profile.removeprefix("profile:")
        profile_root = DeviceProfile.load(profile_name).root
        provision_path = profile_root / "provision.json"
        if not provision_path.is_file():
            raise ValueError(
                f"profile {profile_name!r} has no firmware variant metadata; pass --variant flash|sd"
            )
        provision = json.loads(provision_path.read_text(encoding="utf-8"))
        candidates = [provision.get("variant")]
        firmware = provision.get("firmware")
        if isinstance(firmware, dict):
            candidates.append(firmware.get("variant"))
        retro_go = provision.get("retroGo")
        if isinstance(retro_go, dict) and isinstance(retro_go.get("firmware"), dict):
            candidates.append(retro_go["firmware"].get("variant"))
        variants = {item for item in candidates if item in {"flash", "sd"}}
        if len(variants) != 1:
            raise ValueError(
                f"profile {profile_name!r} does not identify one Retro-Go storage variant; "
                "pass --variant flash|sd"
            )
        profile_variant = variants.pop()
        if variant and variant != profile_variant:
            raise ValueError(
                f"--variant {variant} conflicts with profile {args.profile} ({profile_variant})"
            )
        variant = profile_variant
    if not variant:
        raise ValueError("choose a profile to derive SD_CARD, or pass --variant flash|sd")

    result = build(
        path=args.path,
        clean=args.clean,
        variant=variant,
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
    metadata = stage_build(result, args.output)
    print(json.dumps({"build": asdict(result), "staging": metadata}, indent=2, default=str))
    return 0


def _firmware_install(args) -> int:
    from gwprov.dist.firmware import install_firmware

    result = install_firmware(
        args.output,
        variant=args.variant,
        repo=args.repo,
        version=args.version,
        versions_url=getattr(args, "versions_url", None),
    )
    print(f"Installed Retro-Go {result['version']} ({args.variant}) in {args.output}")
    return 0


def _backups(args) -> int:
    from gwprov import backups
    from rich.table import Table

    if args.backup_action == "add":
        rows = backups.add(args.sources)
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
        table.add_row(
            row["model"].title(),
            f"{row['bank1Bytes']:,} bytes",
            f"{row['bank0Bytes']:,} bytes",
            row["directory"],
        )
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
    for path in result["files"]:
        print(f"  {path}")
    return 0


def _profile_create(args) -> int:
    from gwprov.profiles import profile_destination, profile_reference, validate_resource_name

    if args.name:
        validate_resource_name(args.name, kind="profile name")
    destination = profile_destination(args.directory, output_dir=args.output_dir)
    destination_label = profile_reference(destination, profile_dir=args.output_dir)
    model = "mario" if args.mario else "zelda" if args.zelda else None
    if model:
        from gwprov.backups import list_backups
        from gwprov.stock import create_stock_profile

        backups = list_backups(model)
        if not backups:
            raise ValueError(
                f"no discovered {model.title()} backup pair; run `gwprov backups add DIRECTORY`"
            )
        result = create_stock_profile(
            destination,
            backup_dirs=[row["directory"] for row in backups],
            model=model,
            extflash_mib=args.extflash_mib or 64,
        )
        if args.patched or args.bootloader:
            # Apply the stock transformation before adding Retro-Go's bank 2
            # image. Stock-only profiles start with an explicit erased bank 2
            # map entry, which would otherwise overlap the incoming firmware
            # and abort profile creation before the patch step was reached.
            from gwprov.vendor.qemu_gnw import cfw_images
            import hashlib
            import shutil
            import yaml

            out = destination / ".patch-output"
            backup_dir = Path(backups[0]["directory"])
            cfw_images.main(
                [
                    model,
                    "--backup-dir",
                    str(backup_dir),
                    "--output-dir",
                    str(out),
                    "--extflash-mib",
                    str(args.extflash_mib or 64),
                    *([] if args.bootloader else ["--no-bootloader"]),
                ]
            )
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
                        "--sd-bootloader-file for the Game+Left bootloader at 0x08032000"
                    )
                from gwprov.bootloader import resolve_sd_bootloader

                cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
                sd_bootloader, sd_bootloader_info = resolve_sd_bootloader(
                    cache=cache_home / "gwprov",
                    repo=args.bootloader_repo,
                    version=args.bootloader_version,
                    local=args.sd_bootloader_file,
                )
                bootloader_offset = 0x32000
                if len(bank1) < 0x40000 or any(
                    byte != 0xFF for byte in bank1[bootloader_offset:0x40000]
                ):
                    raise ValueError(
                        "patched bank 1 does not leave the 0x08032000 bootloader region erased"
                    )
                prefix = bank1[:bootloader_offset]
                prefix_path = destination / "blobs" / "stock-bank1-patched.bin"
                bootloader_path = destination / "blobs" / "sd-bootloader.bin"
                prefix_path.write_bytes(prefix)
                bootloader_path.write_bytes(sd_bootloader)
                bank1_components = [
                    {
                        "name": f"Stock {model.title()} (+patch)",
                        "file": prefix_path.relative_to(destination).as_posix(),
                        "offset": 0,
                        "size": len(prefix),
                        "sha256": hashlib.sha256(prefix).hexdigest(),
                    },
                    {
                        "name": f"SD bootloader ({sd_bootloader_info.get('version') or 'local'})",
                        "file": bootloader_path.relative_to(destination).as_posix(),
                        "offset": bootloader_offset,
                        "size": len(sd_bootloader),
                        "sha256": sd_bootloader_info["sha256"],
                    },
                ]
                bank1 = (
                    prefix
                    + sd_bootloader
                    + b"\xff" * (256 * 1024 - bootloader_offset - len(sd_bootloader))
                )
            else:
                if args.sd_bootloader_file:
                    raise ValueError("--sd-bootloader-file requires --bootloader/-b")
                if args.bootloader_file:
                    raise ValueError(
                        "--bootloader-file is for content-based profiles; it is not used by stock patch profiles"
                    )
                bank1_components = [
                    {
                        "name": f"Stock {model.title()} (+patch)",
                        "file": "blobs/stock-bank1.bin",
                        "offset": 0,
                        "size": len(bank1),
                        "sha256": hashlib.sha256(bank1).hexdigest(),
                    }
                ]
            # The patcher pads external flash to the selected chip capacity.
            # Keep only the stock asset range in the component map so later
            # Retro-Go extflash components can be mapped at non-overlapping offsets.
            bank0 = patched_extflash[:asset_size]
            (destination / "bank1.bin").write_bytes(bank1)
            (destination / "extflash.bin").write_bytes(
                bank0 + b"\xff" * (args.extflash_mib * 1024 * 1024 - len(bank0))
            )
            if not args.bootloader:
                (destination / "blobs" / "stock-bank1.bin").write_bytes(bank1)
            (destination / "blobs" / "stock-assets.bin").write_bytes(bank0)
            flash_map_path = destination / "flash-map.yaml"
            flash_map = yaml.safe_load(flash_map_path.read_text())
            flash_map["bank1"] = bank1_components
            flash_map["bank0"] = [
                {
                    "name": f"{model.title()} Assets",
                    "file": "blobs/stock-assets.bin",
                    "offset": 0,
                    "size": len(bank0),
                    "sha256": hashlib.sha256(bank0).hexdigest(),
                }
            ]
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
            import yaml
            import shutil

            source, tag = resolve_release(args.retro_go)
            with tempfile.TemporaryDirectory(prefix="gwprov-retro-profile-") as work:
                content = Path(work) / "content"
                install_firmware(
                    content,
                    variant="flash",
                    repo=source["repo"],
                    version=tag,
                    versions_url=source["versionsUrl"],
                )
                assembled = Path(work) / "assembled"
                create_profile(
                    assembled,
                    content=content,
                    littlefs_mib=args.littlefs_mib,
                    extflash_mib=args.extflash_mib,
                )
                mapping = yaml.safe_load((assembled / "flash-map.yaml").read_text())
                # A stock profile's erased-bank placeholder is a real mapped
                # component. Remove it before placing Retro-Go at bank 2.
                stock_map_path = destination / "flash-map.yaml"
                stock_map = yaml.safe_load(stock_map_path.read_text())
                erased = next(
                    (
                        item
                        for item in stock_map.get("bank2", [])
                        if item.get("name") == "Erased (0xFF)"
                    ),
                    None,
                )
                if erased:
                    edit_profile_blob(
                        destination, action="remove", bank="bank2", name=erased["name"]
                    )
                for bank in ("bank2", "bank0"):
                    for item in mapping.get(bank, []):
                        blob = assembled / item["file"]
                        copied = destination / "blobs" / f"retro-{bank}-{blob.name}"
                        copied.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(blob, copied)
                        edit_profile_blob(
                            destination,
                            action="add",
                            bank=bank,
                            name=item["name"],
                            source=copied,
                            offset=item["offset"],
                        )
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
        _configure_profile_sdcard(
            destination, image=args.sdcard, size_mib=args.sdcard_size, disabled=args.no_sdcard
        )
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
        install_firmware(
            content_root,
            variant="flash",
            repo=source["repo"],
            version=tag,
            versions_url=source["versionsUrl"],
        )
    if not content_root:
        raise ValueError(
            "profile create requires -m/--mario, -z/--zelda, --retro-go SOURCE:VERSION, or --content DIR"
        )
    if args.patched or args.bootloader:
        raise ValueError("--patched and --bootloader require a Mario or Zelda stock profile")
    from gwprov.provision import create_profile

    report = create_profile(
        destination,
        content=content_root,
        name=args.name,
        littlefs_mib=args.littlefs_mib,
        extflash_mib=args.extflash_mib,
        sd_size_mib=args.sdcard_size or 128,
        sd_label=args.sd_label,
        bootloader_repo=args.bootloader_repo,
        bootloader_version=args.bootloader_version,
        bootloader_file=args.bootloader_file,
    )
    if args.no_sdcard and report.get("variant") == "sd":
        raise ValueError("--no-sdcard cannot be used with SD firmware content")
    _configure_profile_sdcard(
        destination, image=args.sdcard, size_mib=args.sdcard_size, disabled=args.no_sdcard
    )
    if report.get("variant") == "sd":
        print(
            f"Created {destination_label}: SD image {report['layout']['sdImageBytes']} bytes; "
            f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash"
        )
    else:
        print(
            f"Created {destination_label}: FrogFS {report['layout']['frogfsBytes']} bytes; "
            f"LittleFS {report['layout']['littlefsBytes']} bytes; "
            f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash"
        )
    print(f"Start with: gwprov start profile:{destination_label}")
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
        destination = profile_reference(result["destination"], profile_dir=args.output_dir)
        console.print(f"  [dim]To[/]    [bold cyan]{destination}[/]")
        console.print(f"  [dim]Files[/] {result['files']}")
        console.print(f"\nStart it with [bold]gwprov start profile:{destination}[/]")
    return 0


def _profile_blob(args) -> int:
    from gwprov.profiles import edit_profile_blob, profile_reference

    result = edit_profile_blob(
        args.profile,
        action=args.action,
        bank=args.bank,
        name=args.name,
        source=getattr(args, "file", None),
        offset=getattr(args, "offset", None),
        replace=getattr(args, "replace", False),
    )
    if args.output == "json":
        print(json.dumps(result, indent=2))
    else:
        console = Console()
        label = profile_reference(result["profile"])
        if result["action"] == "removed":
            console.print(f"[green]Removed[/] {result['name']} from {label} ({result['bank']})")
        else:
            console.print(
                f"[green]{result['action'].title()}[/] {result['name']} ({result['bank']}) "
                f"at 0x{result['offset']:X} ({_human_size(result['size'])})"
            )
            console.print(f"  Profile: {label}")
    return 0


def _configure_profile_sdcard(
    profile_root: str | Path,
    *,
    image: str | None = None,
    size_mib: int | None = None,
    disabled: bool = False,
) -> dict:
    """Set the bundled SD dependency in a profile manifest.

    The profile always keeps the image at ``sdcard.img``.  An existing image
    may be copied in, or a new sparse/partitioned image may be created by the
    shared SD image helper.  ``disabled`` removes the dependency and its local
    image.
    """
    from gwprov.profiles import DeviceProfile

    root = Path(profile_root).expanduser().resolve()
    profile = DeviceProfile.load(root)
    manifest = root / "profile.toml"
    if disabled and (image is not None or size_mib is not None):
        raise ValueError("--no-sdcard cannot be combined with --sdcard or --sdcard-size")
    if image is not None and size_mib is not None:
        raise ValueError("--sdcard IMAGE and --sdcard-size cannot be combined")
    if disabled:
        if (
            profile.resolved_sd
            and profile.resolved_sd.is_file()
            and profile.resolved_sd.parent == root
        ):
            profile.resolved_sd.unlink()
        text = manifest.read_text(encoding="utf-8")
        prefix = text.split("[sd]", 1)[0].rstrip() + "\n"
        manifest.write_text(prefix + '[sd]\nmode = "none"\n', encoding="utf-8")
        return {"mode": "none"}
    if image is None and size_mib is None:
        return {
            "mode": profile.sd_mode,
            "image": str(profile.resolved_sd) if profile.resolved_sd else None,
        }
    destination = root / "sdcard.img"
    if image is not None:
        source = Path(image).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"SD card image does not exist: {source}")
        import shutil

        shutil.copyfile(source, destination)
    else:
        requested_bytes = size_mib * 1024 * 1024
        # SD firmware profile creation already creates and populates this image
        # from the staged sd/ tree. Applying the same requested size here must
        # not replace that populated filesystem with a blank image.
        if not destination.is_file() or destination.stat().st_size != requested_bytes:
            from gwprov.common.sdcard import create_image

            destination.parent.mkdir(parents=True, exist_ok=True)
            create_image(str(destination), size_mb=size_mib)
    text = manifest.read_text(encoding="utf-8")
    prefix = text.split("[sd]", 1)[0].rstrip() + "\n"
    manifest.write_text(prefix + '[sd]\nmode = "bundled"\nimage = "sdcard.img"\n', encoding="utf-8")
    return {"mode": "bundled", "image": str(destination), "size": destination.stat().st_size}


def _profile_edit(args) -> int:
    if args.action in {"add", "remove"}:
        return _profile_blob(args)
    if not (args.sdcard or args.no_sdcard or args.sdcard_size is not None):
        raise ValueError("profile edit requires add/remove or an SD option")
    result = _configure_profile_sdcard(
        args.profile, image=args.sdcard, size_mib=args.sdcard_size, disabled=args.no_sdcard
    )
    from gwprov.profiles import profile_reference

    if args.output == "json":
        print(
            json.dumps(
                {"profile": str(profile_reference(args.profile)), "sdcard": result}, indent=2
            )
        )
    else:
        label = profile_reference(args.profile)
        if result.get("mode") == "none":
            print(f"SD card disabled for {label}")
        else:
            print(f"SD card configured for {label}: {result.get('image')}")
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

    table = Table(
        title=f"[bold bright_cyan]Device profiles ({len(rows)})[/]",
        title_justify="left",
        expand=True,
        show_lines=False,
    )
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


def _active_gwemu_profile(profile: str | None) -> str:
    if profile:
        return profile
    raise ValueError("GWemu operation requires --profile")


def _device_runtime_control(args) -> int:
    row = _resolve_device_selector(args.device)
    if row.get("kind") == "remote-gdb":
        raise ValueError(
            f"{args.action} is not supported for a raw GDB endpoint; it has no GWProv device-control channel"
        )
    if row.get("kind") != "gwemu" and args.action != "reset":
        raise ValueError(f"{args.action} is available for managed GWemu devices only")
    if args.action in {"pause", "halt", "resume"}:
        from gwprov.gwemu_manager import set_instance_running

        return set_instance_running(
            row.get("profile"), pid=row.get("pid"), running=args.action == "resume"
        )
    if args.action == "reset":
        if row.get("kind") == "hardware":
            device_id = str(row.get("id", ""))
            if row.get("probeId") or device_id.startswith("probe:"):
                from gwprov.backends import SelectedPyOCDBackend

                backend = SelectedPyOCDBackend(
                    row.get("probeId") or device_id.removeprefix("probe:"),
                    operation="gwprov device reset",
                    resume_on_disconnect=False,
                )
            elif device_id.startswith("remote:"):
                from gwprov.backends import WebSocketBackend

                backend = WebSocketBackend(
                    device_id.removeprefix("remote:"),
                    origin=row.get("remoteOrigin"),
                    operation="gwprov device reset",
                )
            else:
                raise ValueError("this hardware connection does not support reset")
            try:
                backend.open()
                backend.reset()
            finally:
                backend.close()
            print(f"Reset {row.get('displayName') or row.get('name') or device_id}.")
            return 0
        socket = row.get("qmpSocket")
        if not socket:
            raise ValueError("GWemu device has no QMP control channel")
        from gwprov.gwemu_manager import _qmp_execute

        _qmp_execute(socket, "system_reset")
        print(f"Reset GWemu {row.get('displayName') or row.get('name') or row['id']}.")
        return 0
    raise ValueError(f"unsupported device action: {args.action}")


def _device_logs(args) -> int:
    if args.lines < 1:
        raise ValueError("--lines must be positive")
    row = _resolve_device_selector(args.device)
    if row.get("kind") != "gwemu":
        raise ValueError(
            "logs currently supports managed GWemu devices; use debug python for hardware logs"
        )
    profile = row.get("profile")
    if not profile:
        raise ValueError("GWemu device has no profile log path")
    path = Path(profile) / "gwemu.log"
    if not path.is_file():
        raise ValueError(f"GWemu log is not available: {path}")
    if args.follow:
        with path.open("r", errors="replace") as stream:
            stream.seek(0, os.SEEK_END)
            while True:
                line = stream.readline()
                if line:
                    print(line, end="")
                elif row.get("status") in {"exited", "unknown"}:
                    break
                else:
                    time.sleep(0.25)
        return 0
    lines = path.read_text(errors="replace").splitlines()
    for line in lines[-args.lines :]:
        print(line)
    return 0


def _device_screenshot(args) -> int:
    row = _resolve_device_selector(args.device)
    if row.get("kind") != "gwemu":
        raise ValueError("screenshot currently supports managed GWemu devices")
    from gwprov.gwemu_manager import screenshot_instance

    return screenshot_instance(pid=row.get("pid"), profile=row.get("profile"), output=args.output)


def _device_stop(args) -> int:
    row = _resolve_device_selector(args.device)
    if row.get("kind") != "gwemu":
        raise ValueError("stop currently supports managed GWemu devices")
    from gwprov.gwemu_manager import stop_instance

    return stop_instance(pid=row["pid"], timeout=args.timeout)


def _serve_device(args) -> int:
    from gwprov import serve_manager

    if args.action == "start":
        row = _resolve_device_selector(args.device)
        result = serve_manager.start(
            row, bind=args.bind, port=args.port, gdb_endpoint=args.gdb_endpoint
        )
        print(f"Serving {row.get('displayName') or row.get('name') or row['id']}")
        print(f"  Endpoint: {result['url']}")
        print(f"  Process:  {result['pid']}")
    else:
        try:
            row = _resolve_device_selector(args.device)
            device_id = row["id"]
            display = row.get("displayName") or row.get("name") or device_id
        except ValueError:
            device_id = args.device.removeprefix("device:")
            display = device_id
        result = serve_manager.stop(device_id)
        print(f"Stopped serve session for {display}")
        print(f"  Endpoint: {result['url']}")
    return 0


def _managed_gwemu_profile(args) -> str:
    row = _resolve_device_selector(args.device)
    if row.get("kind") != "gwemu":
        raise ValueError(
            f"{getattr(args, 'action', 'this operation')} currently supports managed GWemu devices"
        )
    profile = row.get("profile") or row.get("assignedProfile")
    if not profile:
        raise ValueError("device has no managed profile")
    args.profile = profile
    return profile


def _device_perf(args) -> int:
    row = _resolve_device_selector(args.device)
    if row.get("kind") == "gwemu":
        _managed_gwemu_profile(args)
        return _gwemu_profile(args)
    return _profile_hardware(args)


def _device_diagnose(args) -> int:
    _managed_gwemu_profile(args)
    return _gwemu_diagnose(args)


def _device_watch(args) -> int:
    _managed_gwemu_profile(args)
    return _gwemu_watch(args)


def _start_profile(args) -> int:
    target_kind, target_value = _resolve_profile_or_device_target(args.target)
    if target_kind == "device":
        row = _resolve_device_selector(target_value)
        if row.get("kind") != "gwemu":
            raise ValueError(
                "start applies to profiles and managed GWemu devices; it cannot start a hardware device"
            )
        if (
            args.name
            or args.gdb_port is not None
            or args.audio
            or args.headless
            or args.timeline
            or args.record_timeline
            or args.timing_mode != "default"
            or args.icount is not None
            or args.pacing is not None
            or args.rtc_epoch is not None
            or args.gwemu_bin
        ):
            raise ValueError("GWemu launch options apply only when TARGET is profile:NAME")
        from gwprov.gwemu_manager import set_instance_running

        result = set_instance_running(row.get("profile"), pid=row.get("pid"), running=True)
        print(f"Started {row.get('displayName') or row.get('name') or row['id']}.")
        return result
    args.profile = target_value
    from gwprov.gwemu_manager import start_instance, instances
    from gwprov.profiles import DeviceProfile

    if args.name:
        from gwprov.profiles import validate_resource_name

        validate_resource_name(args.name, kind="device name")
    start_instance(
        args.profile,
        audio=args.audio,
        gdb_port=args.gdb_port,
        headless=args.headless,
        timeline=args.timeline,
        record_timeline=args.record_timeline,
        timing_mode=args.timing_mode,
        icount=args.icount,
        pacing=args.pacing,
        rtc_epoch=args.rtc_epoch,
        gwemu_bin=args.gwemu_bin,
    )
    profile_root = str(DeviceProfile.load(args.profile).root)
    row = next((item for item in instances() if item.get("profile") == profile_root), None)
    if row is None:
        raise RuntimeError("GWemu started but did not appear in managed inventory")
    device_id = f"gwemu:{row['pid']}"
    if args.name:
        from gwprov.device_assignments import set_assignment

        set_assignment(device_id, "name", args.name)
    print(f"Device: {args.name or Path(profile_root).name}-{1:02d} ({device_id})")
    return 0


def _duplicate_device(args) -> int:
    source = _resolve_device_selector(args.device)
    profile_path = source.get("assignedProfile") or source.get("profile")
    if not profile_path:
        raise ValueError("device has no managed profile to clone")
    from gwprov.profiles import duplicate_profile, profile_destination

    if args.part:
        from gwprov.profiles import DeviceProfile

        profile = DeviceProfile.load(profile_path)
        parts = {
            "bank1": profile.bank1,
            "bank2": profile.bank2,
            "bank0": profile.extflash,
            "extflash": profile.extflash,
            "sdcard": profile.resolved_sd,
        }
        source_path = parts[args.part]
        if source_path is None or not source_path.is_file():
            raise ValueError(f"device profile part is missing: {source_path}")
        if not args.output:
            raise ValueError("--output is required when copying a device part")
        destination = Path(args.output).expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        import shutil

        shutil.copyfile(source_path, destination)
        print(
            f"Copied {args.part} from {source.get('displayName') or source['id']} to {destination}"
        )
        return 0
    if not args.gwemu:
        raise ValueError(
            "choose --gwemu to clone a device, or --part PART --output FILE to copy one part"
        )
    from gwprov.daemon import start_instance
    from gwprov.profiles import profile_reference
    import secrets

    name = args.name or f"{Path(profile_path).name}-clone-{secrets.token_hex(3)}"
    destination = profile_destination(name, output_dir=args.output_dir)
    duplicate_profile(profile_path, destination, output_dir=args.output_dir)
    row = start_instance(str(destination), headless=args.headless, gdb_port=args.gdb_port)
    print(
        f"Cloned {source.get('displayName') or source['id']} to GWemu {profile_reference(destination)} (pid {row['pid']})."
    )
    return 0


def _gwemu_profile(args) -> int:
    from gwprov.profiling import profile_instance

    return profile_instance(
        _active_gwemu_profile(args.profile),
        duration=args.duration,
        interval=args.interval,
        symbols=args.symbols,
        progress_symbols=args.progress_symbol,
        rebase_symbols=args.rebase,
        output=args.output,
        output_format=args.format,
        top=args.top,
        debug_config=args.debug_config,
        stall_threshold=args.stall_threshold,
        progress_interval=args.progress_interval,
    )


def _gwemu_diagnose(args) -> int:
    from gwprov.gwemu_manager import diagnose_instance

    return diagnose_instance(
        _active_gwemu_profile(args.profile),
        symbols=args.symbols,
        output=args.output,
        max_frames=args.max_frames,
        inspect_u32=args.u32,
        inspect_deref=args.deref,
        inspect_bytes=args.bytes,
        inspect_values=args.value,
        inspect_rings=args.ring,
        debug_config=args.debug_config,
    )


def _gwemu_watch(args) -> int:
    from gwprov.gwemu_manager import watch_instance

    return watch_instance(
        _active_gwemu_profile(args.profile),
        symbols=args.symbols,
        progress_symbols=args.progress_symbol,
        guest_pc_symbols=args.guest_pc_symbol,
        rebase_symbols=args.rebase,
        watch_u32=args.u32,
        watch_deref=args.deref,
        watch_bytes=args.bytes,
        watch_values=args.value,
        watch_rings=args.ring,
        heartbeat_symbol=args.heartbeat_symbol,
        frame_symbol=args.frame_symbol,
        interval=args.interval,
        stall_after=args.stall_after,
        duration=args.duration,
        output=args.output,
        debug_config=args.debug_config,
    )


def _ps(args) -> int:
    from gwprov.devices import show_devices

    return show_devices(
        output=args.output, profile=args.profile, refresh=args.refresh, no_pager=args.no_pager
    )


def _resolve_device_selector(selector: str) -> dict:
    """Resolve a device ID or display/name selector without opening hardware."""
    from gwprov.devices import device_rows

    if selector.startswith("device:"):
        selector = selector[len("device:") :]
    elif selector.startswith("profile:"):
        raise ValueError("expected a device target, got a profile target; use device:NAME")
    rows = device_rows()
    folded = selector.casefold()
    matches = [
        row
        for row in rows
        if str(row.get("id", "")).casefold() == folded
        or str(row.get("displayName", "")).casefold() == folded
        or str(row.get("name", "")).casefold() == folded
    ]
    if not matches:
        try:
            from gwprov.profiles import list_profiles

            if any(row["name"].casefold() == folded for row in list_profiles()):
                raise ValueError(
                    f"{selector!r} is a profile; prefix it with `profile:` when a command accepts profiles"
                )
        except ImportError:
            pass
        raise ValueError(
            f"device {selector!r} is not currently available; use `gwprov device ls` to list devices"
        )
    if len(matches) > 1:
        choices = ", ".join(str(row["id"]) for row in matches)
        raise ValueError(f"device name {selector!r} is ambiguous; select one by ID: {choices}")
    return matches[0]


def _resolve_profile_or_device_target(selector: str) -> tuple[str, str]:
    """Resolve a target; bare names are devices and profiles require profile:."""
    if selector.startswith("device:"):
        return "device", selector[len("device:") :]
    if selector.startswith("profile:"):
        return "profile", selector[len("profile:") :]
    return "device", selector


def _retro_go_config(args) -> int:
    """Read or modify Retro-Go /CONFIG with inline options.

    A profile target changes the managed image.  A device target changes its
    assigned profile and deploys only the LittleFS region after confirmation.
    With no mutation flags the operation is read-only.
    """
    from gwprov.common.retrogo_config import build_from_values, rom_path
    from gwprov.device_config import decode_config, profile_config_target, read_config
    from gwprov.profiles import DeviceProfile, profile_reference

    if not args.target:
        raise ValueError("retro-go config requires profile:NAME or device:NAME")
    if args.rom and (args.auto_start is not None or args.no_auto_start):
        raise ValueError("--rom cannot be combined with --auto-start or --no-auto-start")
    target_kind, target_value = _resolve_profile_or_device_target(args.target)
    target_row = _resolve_device_selector(target_value) if target_kind == "device" else None
    device_id = target_row["id"] if target_row else None
    assigned_profile = (target_row or {}).get("assignedProfile") if target_row else None
    profile_name = target_value if target_kind == "profile" else assigned_profile
    config_target = profile_config_target(profile_name) if profile_name else "flash/ext"
    mutation_requested = any(
        (
            args.auto_start is not None,
            args.no_auto_start,
            args.timeout is not None,
            args.oc_level is not None,
            args.selected_tab is not None,
            args.cursor is not None,
            args.browse_subpath is not None,
        )
    )
    if device_id and not profile_name and mutation_requested:
        raise ValueError(
            "device has no assigned profile; use `gwprov profile PROFILE apply device:DEVICE` first"
        )
    if device_id and target_row.get("kind") == "hardware" and config_target == "sd":
        raise ValueError(
            "this device's Retro-Go /CONFIG is stored on its SD card; edit the managed profile "
            "with `gwprov retro-go config profile:PROFILE`"
        )

    blob, source, _ = read_config(
        device_id=device_id,
        profile=profile_name,
        remote_origin=(target_row or {}).get("remoteOrigin"),
    )
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
        return _print_config_report(
            decoded, source=source, target=device_id or profile_name, output_format=args.format
        )

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

    if device_id:
        if target_row.get("kind") != "hardware":
            raise ValueError(
                "writing Retro-Go CONFIG to GWemu is not supported; update its profile instead"
            )
        _confirm_apply(
            args,
            bool(args.yes),
            [
                f"Target: {target_row.get('displayName') or target_row.get('name') or device_id}",
                f"Profile: {profile_reference(DeviceProfile.load(profile_name).root)}",
                "Action: write /CONFIG and deploy the LittleFS region",
            ],
        )
    if profile_name:
        with tempfile.TemporaryDirectory(prefix="gwprov-config-") as temp_dir:
            temp_path = Path(temp_dir) / "CONFIG"
            temp_path.write_bytes(new_blob)
            from gwprov.filesystem import operate

            operate(
                operation="add",
                target=config_target,
                profile=profile_name,
                filesystem="fatfs" if config_target == "sd" else "littlefs",
                # FAT image additions treat path as a destination directory and
                # preserve the source basename. LittleFS treats it as the exact
                # destination file path.
                path="/" if config_target == "sd" else "CONFIG",
                source=str(temp_path),
            )
    if device_id:
        from gwprov.deploy import apply_deployment

        remote_url = device_id.removeprefix("remote:") if device_id.startswith("remote:") else None
        apply_deployment(
            profile_name,
            probe_id=target_row.get("probeId"),
            remote_url=remote_url,
            remote_origin=target_row.get("remoteOrigin"),
            regions=["littlefs"],
        )
    return _print_config_report(
        decode_config(new_blob),
        source=source,
        target=device_id or profile_name,
        output_format=args.format,
    )


def _print_config_report(decoded: dict, *, source: str, target: str, output_format: str) -> int:
    report = {"schemaVersion": 1, "target": target, "source": source, **decoded}
    valid = all(
        decoded["integrity"][key] for key in ("magic_valid", "version_supported", "crc_valid")
    )
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
    selected = getattr(args, "device", None)
    if not selected:
        raise ValueError("recover requires DEVICE")
    row = _resolve_device_selector(selected)
    device_id = row["id"]
    if device_id.startswith("probe:"):
        from gwprov.backends import SelectedPyOCDBackend

        backend = SelectedPyOCDBackend(
            device_id.removeprefix("probe:"),
            operation="gwprov recover",
            lease_wait=0,
            allow_recovery_marker=True,
            resume_on_disconnect=False,
        )
    elif device_id.startswith("remote:"):
        from gwprov.backends import WebSocketBackend

        backend = WebSocketBackend(
            device_id.removeprefix("remote:"),
            origin=(row or {}).get("remoteOrigin"),
            operation="gwprov recover",
            lease_wait=0,
            allow_recovery_marker=True,
        )
    else:
        raise ValueError("recover requires a physical hardware device")

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
    if args.ids_only or getattr(args, "completion", False):
        from gwprov.devices import device_identifiers

        for identifier in device_identifiers(include_names=getattr(args, "completion", False)):
            print(identifier)
        return 0
    return _resource_view("devices", args)


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
            print("No SD cards registered. Add one with `gwprov media sdcard add PATH [NAME]`.")
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


def _confirm_apply(args, assume_yes: bool, details: list[str], *, writes=None) -> None:
    """Require an explicit go-ahead before apply changes a device or files."""
    if assume_yes:
        return
    from rich.prompt import Confirm

    console = Console(file=sys.stderr, force_terminal=sys.stderr.isatty())
    console.print(_apply_plan_renderable(details, writes or [], console.width))
    if not sys.stdin.isatty():
        raise ValueError("confirmation needs an interactive terminal; rerun with `--yes`")
    try:
        accepted = Confirm.ask("Continue?", default=False, console=console)
    except EOFError as exc:
        raise ValueError("apply was not confirmed; rerun with `--yes` to skip the prompt") from exc
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
            table = Table(
                box=box.SIMPLE, expand=True, padding=(0, 1), show_header=True, header_style="bold"
            )
            table.add_column("FLASH TARGET", style="bright_cyan", overflow="fold")
            table.add_column("IMAGE", overflow="fold")
            table.add_column("SIZE", justify="right", no_wrap=True)
            for row in writes:
                table.add_row(
                    Text(str(row["target"])),
                    Text(str(row["content"])),
                    _human_size(row["bytes"]) if row["bytes"] is not None else "device read",
                )
        else:
            table = Text()
            for index, row in enumerate(writes):
                if index:
                    table.append("\n")
                table.append(str(row["target"]), style="bold bright_cyan")
                table.append("\n  ")
                table.append(str(row["content"]))
                table.append("\n  Size: ", style="dim")
                table.append(
                    _human_size(row["bytes"]) if row["bytes"] is not None else "read from device",
                    style="dim",
                )
        renderables.extend([Text("Planned writes", style="bold"), table])
    return Panel(
        Group(*renderables),
        title="Apply plan",
        border_style="bright_cyan",
        box=box.ROUNDED,
        expand=True,
    )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return (
                f"{int(value)} {unit}"
                if unit == "bytes"
                else f"{value:.1f} {unit}".replace(".0 ", " ")
            )
        value /= 1024


def _resource_view(section: str, args) -> int:
    args.section = section
    if not hasattr(args, "firmware_repo"):
        args.firmware_repo = "sylverb/game-and-watch-retro-go-sd"
    return _show(args)


def _adapters_list(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from gwprov.adapters import inventory

    rows = inventory()
    if args.output == "json":
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        Console().print(
            "[dim]No local adapters or registered remote adapters found. Use `gwprov adapters add NAME URL` to register a gnwmanager or GDB server.[/]"
        )
        return 0
    table = Table(title="Adapters", title_justify="left", expand=True)
    table.add_column("", width=1, no_wrap=True)
    table.add_column("ADAPTER", style="cyan", overflow="fold")
    table.add_column("TYPE", overflow="fold")
    table.add_column("ID", overflow="fold")
    table.add_column("STATE")
    table.add_column("DETAILS", overflow="fold")
    for row in rows:
        table.add_row(
            "",
            row["name"],
            row["adapter"],
            row["id"],
            row["state"].upper(),
            row.get("detail") or "",
        )
    Console().print(table)
    return 0


def _adapters_add(args) -> int:
    from gwprov.adapters import add_gdb, add_remote

    if args.url.casefold().startswith("gdb://"):
        if args.origin:
            raise ValueError("--origin applies only to gnwmanager WebSocket endpoints")
        adapter = add_gdb(args.name, args.url)
    else:
        adapter = add_remote(args.name, args.url, origin=args.origin)
    print(f"Added remote adapter {adapter['name']}: {adapter['url']}")
    if adapter.get("origin"):
        print(f"  Origin: {adapter['origin']}")
    print("Use it with an explicit device target from `gwprov device ls`.")
    return 0


def _adapters_remove(args) -> int:
    from gwprov.adapters import remove_remote

    adapter = remove_remote(args.name_or_url)
    print(f"Removed remote adapter {adapter['name']} ({adapter['url']})")
    return 0


def _performance_report_paths() -> list[str]:
    from gwprov.profiles import profile_directory

    roots = [Path("dev-local/reports")]
    profile_root = profile_directory()
    if profile_root.exists():
        roots.extend(path / "runtime/gwprov" for path in profile_root.iterdir() if path.is_dir())
    extensions = {".json", ".html", ".pdf"}
    return sorted(
        {
            str(path)
            for root in roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and path.suffix.casefold() in extensions
        }
    )


def _show(args) -> int:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    from gwprov.devices import device_rows

    section = getattr(args, "section", None)
    categories = {
        "adapters",
        "devices",
        "projects",
        "profile",
        "perf",
        "media",
        "sdcard",
        "performance",
    }
    if section and section not in categories:
        from gwprov.cli.text import device_detail_data, print_device_detail

        row = _resolve_device_selector(section)
        data = device_detail_data(row)
        if args.output == "json":
            print(json.dumps(data, indent=2))
        else:
            print_device_detail(row, console=Console())
        return 2 if data["summary"]["state"] == "Unknown" else 0
    if section == "devices":
        rows = device_rows()
        if args.output == "json":
            print(
                json.dumps(
                    [
                        {
                            k: row.get(k)
                            for k in (
                                "id",
                                "kind",
                                "name",
                                "status",
                                "application",
                                "installed",
                                "assignedProfile",
                                "sdCard",
                            )
                        }
                        for row in rows
                    ],
                    indent=2,
                )
            )
            return 0
        if not rows:
            Console().print(
                "[dim]No devices detected. Connect a probe or start GWemu, then run `gwprov show devices` again.[/]"
            )
            return 0
        table = Table(title="Devices", title_justify="left", expand=True)
        for name in ("DEVICE", "TYPE", "STATE", "RUNNING", "INSTALLED", "PROFILE", "SD CARD"):
            table.add_column(name, overflow="fold")
        for row in rows:
            state = Text(
                str(row["status"]).upper(),
                style={
                    "running": "green",
                    "ready": "green",
                    "halted": "yellow",
                    "recovery": "yellow",
                    "busy": "cyan",
                    "unknown": "red",
                }.get(row["status"], "white"),
            )
            table.add_row(
                row["id"],
                row["kind"],
                state,
                row.get("application", "Unknown"),
                row.get("installed", "Unknown"),
                row.get("assignedProfile", ""),
                row.get("sdCard", ""),
            )
        Console().print(table)
        return 0
    if section == "adapters":
        from gwprov.adapters import inventory

        adapters = {}
        for row in inventory():
            key = row["adapter"]
            adapters.setdefault(key, []).append(row)
        data = [
            {
                "adapter": name,
                "count": len(items),
                "state": ", ".join(sorted({r["state"] for r in items})),
            }
            for name, items in sorted(adapters.items())
        ]
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

        catalog = load_project_catalog(
            getattr(args, "firmware_repo", "sylverb/game-and-watch-retro-go-sd")
        )
        projects = catalog["projects"]
        if args.output == "json":
            print(json.dumps(projects, indent=2))
        else:
            print(
                f"{len(projects)} projects in catalog ({sum(p['kind'] == 'core' for p in projects)} cores, "
                f"{sum(p['kind'] == 'homebrew' for p in projects)} homebrew)"
            )
            print("Browse details with: gwprov project list")
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
    if section in ("media", "sdcard"):
        from gwprov.sd_cards import list_cards

        cards = list_cards()
        if args.output == "json":
            print(json.dumps(cards, indent=2))
        elif not cards:
            print("No SD cards registered. Add one with `gwprov media sdcard add PATH [NAME]`.")
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

        print(
            json.dumps(
                {
                    "devices": len(rows),
                    "adapters": len(adapter_rows),
                    "projects": "available via project list",
                    "profiles": len(profiles),
                    "sdCards": len(list_cards()),
                    "performanceProfiles": len(_performance_report_paths()),
                },
                indent=2,
            )
        )
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
        print("Use `gwprov show devices|adapters|projects|profile|perf|media` for an overview.")
    return 0


def _daemon_restart(args) -> int:
    from gwprov.daemon_ipc import request
    from gwprov.daemon import ensure_running, _stop_stale_daemon, _daemon_identity, _pid_exists

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

        blocking_owners = [
            owner for owner in active_lease_owners() if operation_blocks_drain(owner)
        ]
        cancelled = []
        if blocking_owners and not args.force:
            raise RuntimeError(
                f"stale daemon has {len(blocking_owners)} substantial device operation(s); "
                "wait for them or use `gwprov daemon restart --force` to cancel them"
            )
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
        _stop_stale_daemon(
            int(ping.get("daemonPid", 0)), timeout=min(args.timeout, 8.0), force=args.force
        )
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
            raise RuntimeError(
                "daemon is still draining; a device owner did not exit after cancellation"
            )
        raise RuntimeError(
            f"daemon is draining with {result.get('blockingLeases', 0)} substantial device operation(s); "
            "use `gwprov daemon restart --force` to cancel them"
        )
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
            destination = (
                f"bank {row['bank']}+0x{row['offset']:x}"
                if "bank" in row
                else f"offset 0x{row['offset']:x}"
            )
            print(
                f"  {row['region']:<10} {row['bytes']:>10} bytes  {destination}  sha256={row['sha256']}"
            )
        print("Apply leaves the RAM flasher running unless --start is given.")
        print("Use --start --bank 1 (default) or --start --bank 2 to run a bank after writing.")
        print("SD deployment overlays files and retains existing files.")
    return 0


def _profile_plan(args) -> int:
    args.profile = args.profile_name
    return _deploy_plan(args)


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
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    console = Console(file=sys.stdout, force_terminal=sys.stdout.isatty())
    if sys.stdout.isatty():
        progress = Progress(
            TextColumn("{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            DownloadColumn(),
            TimeElapsedColumn(),
            console=console,
        )
        progress.start()
        task_id = None

        def update(event: dict) -> None:
            nonlocal task_id
            kind = event["event"]
            if kind == "start":
                if task_id is None:
                    task_id = progress.add_task("Preparing deployment", total=event["total_bytes"])
                else:
                    progress.update(
                        task_id,
                        total=event["total_bytes"],
                        completed=0,
                        description="Preparing deployment",
                    )
            elif kind == "device_extflash":
                progress.console.print(_device_extflash_renderable(event["summary"], console.width))
            elif kind == "phase" and task_id is not None:
                progress.update(task_id, description=event["message"])
            elif kind == "region_start" and task_id is not None:
                progress.update(task_id, description=f"Writing {event['region']}")
            elif kind == "progress" and task_id is not None:
                progress.update(task_id, completed=event["overall_done"])
            elif kind == "complete" and task_id is not None:
                progress.update(
                    task_id,
                    completed=event["overall_total"],
                    description=(
                        f"Started {event['booted']}" if event.get("booted") else "RAM flasher ready"
                    ),
                )

        def finish() -> None:
            progress.stop()

        return update, finish

    last_percent = -1

    def update(event: dict) -> None:
        nonlocal last_percent
        kind = event["event"]
        if kind == "start":
            print(
                f"Preparing deployment ({event['region_count']} regions, "
                f"{event['total_bytes'] / (1024 * 1024):.1f} MiB).",
                flush=True,
            )
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
                print(
                    f"  {event['region']}: {percent}% "
                    f"({event['region_done'] / (1024 * 1024):.1f}/"
                    f"{total / (1024 * 1024):.1f} MiB)",
                    flush=True,
                )
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
        table = Table(
            box=box.SIMPLE, expand=True, padding=(0, 1), show_header=True, header_style="bold"
        )
        table.add_column("DEVICE RANGE", style="bright_cyan", overflow="fold")
        table.add_column("CURRENT CONTENT", overflow="fold")
        table.add_column("SIZE", justify="right", no_wrap=True)
        for row in summary["regions"]:
            table.add_row(Text(row["target"]), Text(row["content"]), _human_size(row["bytes"]))
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
    return Panel(
        Group(detail, Text("Current extflash contents", style="bold"), table),
        title="Read from device",
        border_style="bright_cyan",
        box=box.ROUNDED,
        expand=True,
    )


def _deploy_apply(args) -> int:
    from gwprov.deploy import apply_deployment

    probe_id, programmer, remote_url = args.probe_id, args.programmer, args.remote_url
    remote_origin = args.remote_origin
    explicit_device = getattr(args, "device", None)
    if explicit_device and any((probe_id, programmer, remote_url)):
        raise ValueError("use DEVICE or an explicit adapter option, not both")
    if explicit_device:
        row = _resolve_device_selector(explicit_device)
        if row.get("kind") != "hardware":
            raise ValueError("deployment requires a physical hardware device")
        if row.get("probeId"):
            probe_id = row["probeId"]
        elif row["id"].startswith("remote:"):
            remote_url = row["id"].removeprefix("remote:")
            remote_origin = remote_origin or row.get("remoteOrigin")
        else:
            raise ValueError(f"device {explicit_device!r} has no hardware connection")
    elif not any((probe_id, programmer, remote_url)):
        raise ValueError("profile apply requires DEVICE or an explicit adapter option")
    from gwprov.deploy import deployment_plan, deployment_write_summary

    plan = deployment_plan(args.profile, args.region)
    _confirm_apply(
        args,
        bool(getattr(args, "yes", False)),
        [
            f"Profile: {args.profile}",
            f"Target: {explicit_device or 'explicit hardware adapter'}",
            (
                f"After writing: start bank {args.bank} reset vector"
                if args.start
                else "After writing: leave the RAM flasher running"
            ),
        ],
        writes=deployment_write_summary(args.profile, plan),
    )
    callback, finish = _deployment_progress_display(args.output)
    try:
        result = apply_deployment(
            args.profile,
            probe_id=probe_id,
            programmer=programmer,
            remote_url=remote_url,
            remote_origin=remote_origin,
            regions=args.region,
            start=args.start,
            bank=args.bank,
            progress=callback,
        )
    finally:
        if finish:
            finish()
    if args.output in {"json", "yaml"}:
        _print_structured(result, args.output)
    return 0


def _profile_apply(args) -> int:
    args.profile = args.profile_name
    result = _deploy_apply(args)
    if result == 0:
        from gwprov.device_assignments import set_assignment
        from gwprov.profiles import DeviceProfile

        row = _resolve_device_selector(args.device)
        set_assignment(row["id"], "profile", str(DeviceProfile.load(args.profile).root))
    return result


def _profile_hardware(args) -> int:
    from gwprov.hw_profile import profile_hardware

    probe_id, programmer, remote_url = args.probe_id, args.programmer, args.remote_url
    remote_origin = args.remote_origin
    target_row = None
    if args.device:
        target_row = _resolve_device_selector(args.device)
        if target_row.get("kind") != "hardware":
            raise ValueError("perf hardware requires a physical hardware device")
        if target_row.get("probeId"):
            probe_id = target_row["probeId"]
        elif target_row["id"].startswith("remote:"):
            remote_url = target_row["id"].removeprefix("remote:")
            remote_origin = target_row.get("remoteOrigin")
    elif not any((probe_id, programmer, remote_url)):
        raise ValueError("perf hardware requires DEVICE")
    elif probe_id or remote_url:
        from gwprov.devices import device_rows

        target_id = f"probe:{probe_id}" if probe_id else f"remote:{remote_url}"
        target_row = next((item for item in device_rows() if item["id"] == target_id), None)
    elif programmer:
        from gwprov.devices import device_rows
        from gwprov.target_leases import adapter_for_probe

        matches = [
            item
            for item in device_rows()
            if item.get("kind") == "hardware"
            and str(item.get("id", "")).startswith("probe:")
            and adapter_for_probe(f"{item.get('vendor', '')} {item.get('name', '')}") == programmer
        ]
        if len(matches) == 1:
            target_row = matches[0]

    profile = args.profile
    if target_row and not profile:
        import re
        from gwprov.debug_symbols import retro_go_profile_symbols

        match = re.search(
            r"Retro-Go\s+(?:SD\s+)?(v[\w.+-]+)", target_row.get("installed", ""), re.IGNORECASE
        )
        matched = retro_go_profile_symbols(match.group(1)) if match else None
        if matched:
            firmware_elf, _ = matched
            profile = str(firmware_elf.parents[1])
            import sys

            print(
                f"Using exact Retro-Go {match.group(1)} symbols from profile "
                f"{firmware_elf.parents[1].name}",
                file=sys.stderr,
            )
    return profile_hardware(
        probe_id=probe_id,
        programmer=programmer,
        remote_url=remote_url,
        remote_origin=remote_origin,
        profile=profile,
        symbols=args.symbols,
        duration=args.duration,
        interval=args.interval,
        output=args.output,
        output_format=args.format,
        top=args.top,
        counter_symbols=args.counter_symbol,
        frame_counter=args.frame_counter,
        total_cycle_counter=args.total_cycle_counter,
        start_at=args.start_at,
        set_registers=args.set_register,
        startup_timeout=args.startup_timeout,
    )


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
    remote_gdb_endpoints = []
    detected_retro_go = None
    active_row = None
    target = args.target
    gdb_port = args.port
    if args.device:
        if args.device.startswith("device:"):
            args.device = args.device[len("device:") :]
        elif args.device.startswith("profile:"):
            raise ValueError("debug python requires a device target, not a profile")
        if any((probe_ids, programmers, remote_urls, remote_gdb_endpoints)):
            raise ValueError("use either the positional device or explicit adapter options")
        from gwprov.devices import device_rows

        rows = device_rows()
        exact_id = [item for item in rows if item["id"].casefold() == args.device.casefold()]
        exact_name = [
            item for item in rows if item.get("name", "").casefold() == args.device.casefold()
        ]
        matches = exact_id or exact_name
        if not matches:
            raise ValueError(
                f"device {args.device!r} is not currently available; use `gwprov device ls` to list devices"
            )
        if len(matches) > 1:
            choices = ", ".join(item["id"] for item in matches)
            raise ValueError(
                f"device name {args.device!r} is ambiguous; select one by ID: {choices}"
            )
        active_row = matches[0]
        inferred_target = "gwemu" if active_row.get("kind") == "gwemu" else "hardware"
        if target and target != inferred_target:
            raise ValueError(f"device {args.device!r} is a {inferred_target} target, not {target}")
        target = inferred_target
    elif target is None and any((probe_ids, programmers, remote_urls)):
        target = "hardware"
    elif target is None:
        raise ValueError("debug python requires DEVICE or an explicit adapter option")

    if target == "hardware" and not any((probe_ids, programmers, remote_urls)):
        if active_row is None:
            raise ValueError(
                "debug python hardware mode requires DEVICE or --probe-id/--remote-url/--programmer"
            )
        if active_row.get("probeId"):
            probe_ids.append(active_row["probeId"])
        elif active_row["id"].startswith("remote:"):
            remote_urls.append(active_row["id"].removeprefix("remote:"))
            origin = active_row.get("remoteOrigin")
            if origin:
                remote_origins.append(origin)
        elif active_row.get("remoteGdb"):
            remote_gdb_endpoints.append(
                {
                    "name": active_row["name"],
                    "host": active_row["gdbHost"],
                    "port": active_row["gdbPort"],
                    "url": active_row["gdbUrl"],
                }
            )
        else:
            raise ValueError("selected device is not a hardware target")
        import re

        match = re.search(
            r"Retro-Go\s+(?:SD\s+)?(v[\w.+-]+)", active_row.get("installed", ""), re.IGNORECASE
        )
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
                    symbol_paths.extend(
                        path for path in matched_symbols if path not in symbol_paths
                    )
                    print(
                        f"Using installed Retro-Go {detected_retro_go} symbols from profile "
                        f"{firmware_elf.parents[1].name}"
                    )
    if target == "gwemu" and qmp_socket is None and not profile:
        if active_row is None:
            raise ValueError("debug python GWemu mode requires DEVICE or --profile")
        from gwprov.gwemu_manager import instances

        vm = next(
            (item for item in instances() if str(item.get("pid")) == str(active_row.get("pid"))),
            None,
        )
        if vm:
            qmp_socket = vm.get("qmpSocket")
            profile = vm.get("profile") or active_row.get("assignedProfile")
            if gdb_port is None:
                gdb_port = vm.get("gdbPort")
        if qmp_socket is None and profile is None:
            raise ValueError("selected GWemu device has no QMP endpoint or managed profile")
    if profile:
        from gwprov.profiles import DeviceProfile

        profile_root = DeviceProfile.load(profile).root
        firmware_symbols = profile_root / "debug" / "retro-go-debug.elf"
        if firmware_symbols.is_file():
            symbol_paths.insert(0, str(firmware_symbols))
        debug_root = profile_root / "debug"
        app_symbols = (
            sorted((debug_root / "apps").rglob("*.elf")) if (debug_root / "apps").is_dir() else []
        )
        project_symbols = (
            sorted((debug_root / "projects").rglob("*.elf"))
            if (debug_root / "projects").is_dir()
            else []
        )
        symbol_paths[1:1] = [
            str(path) for path in [*app_symbols, *project_symbols] if str(path) not in symbol_paths
        ]
        if (
            not firmware_symbols.is_file()
            and not app_symbols
            and not project_symbols
            and not symbol_paths
        ):
            raise ValueError(f"profile has no bundled debug symbols: {debug_root}")
        if target == "gwemu" and qmp_socket is None:
            from gwprov.gwemu_manager import instances

            matches = [row for row in instances() if row.get("profile") == str(profile_root)]
            if len(matches) == 1:
                qmp_socket = matches[0].get("qmpSocket")
                if gdb_port is None:
                    gdb_port = matches[0].get("gdbPort")
            elif len(matches) > 1:
                raise ValueError(
                    "profile has multiple GWemu instances; select a QMP handle explicitly"
                )
    elif active_row and active_row.get("assignedProfile"):
        from gwprov.profiles import DeviceProfile

        assigned_root = DeviceProfile.load(active_row["assignedProfile"]).root
        project_root = assigned_root / "debug" / "projects"
        if project_root.is_dir():
            for path in sorted(project_root.rglob("*.elf")):
                if str(path) not in symbol_paths:
                    symbol_paths.append(str(path))
    if target == "gwemu" and gdb_port is None:
        raise ValueError(
            "GWemu GDB port was not supplied and could not be inferred; "
            "pass --port or select a managed GWemu instance"
        )
    return python_shell(
        target=target,
        host=args.host,
        port=gdb_port,
        openocd_port=args.openocd_port,
        probe_ids=probe_ids,
        programmers=programmers,
        remote_urls=remote_urls,
        remote_origins=remote_origins,
        remote_gdb_endpoints=remote_gdb_endpoints,
        symbols=symbol_paths,
        qmp_socket=qmp_socket,
        debug_config=args.debug_config,
        retro_go_version=detected_retro_go,
        start_paused=args.paused,
        attach_only=args.attach_only,
        autostart_timeout=args.autostart_timeout,
        watchdog_freeze=not args.no_watchdog_freeze,
    )


def _ofw_patch(args) -> int:
    from gwprov.vendor.qemu_gnw import cfw_images

    forwarded = [
        args.game,
        "--backup-dir",
        args.backup_dir,
        "--output-dir",
        args.output_dir,
        "--extflash-mib",
        str(args.extflash_mib),
        *(["--no-bootloader"] if args.no_bootloader else []),
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
    row = _resolve_device_selector(args.device)
    if row.get("probeId"):
        from gwprov.backends import SelectedPyOCDBackend
        from gwprov.target_leases import adapter_for_probe

        backend = SelectedPyOCDBackend(
            row["probeId"],
            operation="gwprov input transport",
            probe_adapter=adapter_for_probe(f"{row.get('vendor', '')} {row.get('name', '')}"),
        )
    elif row["id"].startswith("remote:"):
        from gwprov.backends import WebSocketBackend

        backend = WebSocketBackend(
            row["id"].removeprefix("remote:"),
            origin=row.get("remoteOrigin"),
            operation="gwprov input transport",
        )
    else:
        raise ValueError("input tap requires a physical hardware device")
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
            raise ValueError("--image requires --filesystem")
        if args.filesystem == "littlefs":
            if not args.size:
                raise ValueError("LittleFS inventory requires --size")
            entry = littlefs(image, args.offset, args.size, args.block_size)
        else:
            entry = (frogfs if args.filesystem == "frogfs" else fatfs)(image, args.offset)
        result = {"schemaVersion": 1, "images": {}, "filesystems": [entry]}
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text, end="")
    return 0


def _filesystem(args) -> int:
    from gwprov.filesystem import operate

    action = getattr(args, "fs_command", None) or getattr(args, "media_command", None)
    operation = "ls" if action in ("ls", "dir") else action
    filesystem = getattr(args, "filesystem", None)
    image = getattr(args, "image", None)
    profile = getattr(args, "profile", None)
    device = getattr(args, "device", None)
    size = getattr(args, "size", None)
    if device and device.startswith("profile:"):
        if profile:
            raise ValueError("specify one media target")
        profile = device[len("profile:") :]
        device = None
    elif device and device.startswith("device:"):
        device = device[len("device:") :]
    if action in {"add", "delete", "ls", "dir", "tree"}:
        if action == "add" and getattr(args, "abstract_media_add", False):
            source = getattr(args, "source", None) or "."
            destination = getattr(args, "dest", "/")
            sdcard = bool(getattr(args, "sdcard", False))
            contiguous = bool(getattr(args, "contiguous", False))
            sentinel = getattr(args, "sentinel", None)
            if sdcard and (contiguous or sentinel is not None):
                raise ValueError("--sd/--sdcard cannot be combined with --contiguous or --sentinel")
            if sentinel is not None and Path(source).expanduser().is_dir():
                raise ValueError("--sentinel cannot be used with a directory source")
            if sentinel is not None and not 0 <= sentinel <= 0xFFFFFFFF:
                raise ValueError("--sentinel address must fit in 32 bits")
            if device:
                target_text = str(device)
                blob_hint = None
                if target_text.startswith("blob:"):
                    target_text = target_text[5:]
                    if "=" in target_text:
                        blob_hint, target_text = target_text.split("=", 1)
                    elif target_text in {"frogfs", "littlefs", "sd", "sdcard"}:
                        blob_hint = target_text
                    if blob_hint in {"sd", "sdcard"}:
                        blob_hint = "sd"
                image_path = Path(target_text).expanduser() if target_text else None
                if image_path is not None and image_path.is_file():
                    from gwprov.filesystem import identify_filesystem_image

                    detected = identify_filesystem_image(image_path, hint=blob_hint)
                    if detected == "sd" and (contiguous or sentinel is not None):
                        raise ValueError(
                            "SD images cannot be combined with --contiguous or --sentinel"
                        )
                    if detected == "littlefs" and contiguous:
                        raise ValueError("--contiguous cannot be used with a LittleFS image")
                    if contiguous and detected != "frogfs":
                        raise ValueError(
                            "--contiguous requires a valid FrogFS image; "
                            f"this blob is a valid {detected} image"
                        )
                    if sentinel is not None and detected != "frogfs":
                        raise ValueError("--sentinel can only be used with a FrogFS image")
                    if sdcard and detected != "sd":
                        raise ValueError("--sd/--sdcard requires a valid FAT SD image")
                    filesystem = "sd" if detected == "sd" else detected
                    return operate(
                        operation="add",
                        target="sd" if filesystem == "sd" else "flash/ext",
                        image=str(image_path),
                        filesystem=filesystem,
                        offset=getattr(args, "offset", 0),
                        size=image_path.stat().st_size,
                        block_size=getattr(args, "block_size", 4096),
                        path=destination,
                        source=source,
                        contiguous=contiguous,
                        sentinel=sentinel,
                    )
                if blob_hint is not None:
                    raise ValueError(
                        "blob:TYPE requires a filesystem image path; use `blob:TYPE PATH`"
                    )
                if image_path is not None and (
                    "/" in target_text
                    or "\\" in target_text
                    or image_path.suffix.casefold() in {".bin", ".img", ".qcow2"}
                ):
                    raise ValueError(
                        f"filesystem image does not exist or is not a file: {image_path}"
                    )
                row = _resolve_device_selector(device)
                assigned = row.get("assignedProfile")
                variant = None
                if assigned:
                    from gwprov.profiles import DeviceProfile

                    variant = _infer_project_variant_from_profile(DeviceProfile.load(assigned).root)
                if not variant:
                    observed = str(row.get("application", "")).casefold()
                    installed = str(row.get("installed", "")).casefold()
                    detected = observed + " " + installed
                    if "retro-go sd" in detected:
                        variant = "sd"
                    elif "retro-go" in detected:
                        variant = "flash"
                if variant == "sd" and (contiguous or sentinel is not None):
                    raise ValueError(
                        "--contiguous and --sentinel cannot be used with an SD Retro-Go device"
                    )
                normalized = destination.replace("\\", "/").strip("/").casefold()
                source_path = Path(source).expanduser()
                source_name = source_path.name.casefold() if source_path.is_dir() else ""
                roots = {"bios", "fonts", "homebrews"}
                first = normalized.split("/", 1)[0] if normalized else ""
                inferred_frogfs = (
                    contiguous or sentinel is not None or first in roots or source_name in roots
                )
                if not sdcard and variant is None and not inferred_frogfs:
                    raise ValueError(
                        "cannot determine device storage from its observed firmware; "
                        "assign a profile, use --sdcard, or select a FrogFS path"
                    )
                if sdcard or (variant == "sd" and not contiguous and sentinel is None):
                    filesystem = "sd"
                else:
                    filesystem = "frogfs" if inferred_frogfs else "littlefs"
                _confirm_apply(
                    args,
                    bool(getattr(args, "yes", False)),
                    [
                        f"Target: {device}",
                        f"Storage: {'SD card' if filesystem == 'sd' else 'FrogFS' if filesystem == 'frogfs' else 'LittleFS'}",
                        f"Source: {Path(source).expanduser()}",
                        f"Destination: {destination}",
                    ],
                )
                from gwprov.device_media import operate_device

                return operate_device(
                    device=device,
                    operation="add",
                    filesystem=filesystem,
                    path=destination,
                    source=source,
                    contiguous=contiguous or sentinel is not None,
                    sentinel=sentinel,
                    yes=bool(getattr(args, "yes", False)),
                )
            if profile:
                from gwprov.profiles import DeviceProfile

                selected = DeviceProfile.load(profile)
                variant = _infer_project_variant_from_profile(selected.root)
                if variant == "sd" and (contiguous or sentinel is not None):
                    raise ValueError(
                        "--contiguous and --sentinel cannot be used with an SD Retro-Go profile"
                    )
                if sdcard:
                    filesystem = "sd"
                elif variant == "sd" and not contiguous and sentinel is None:
                    filesystem = "sd"
                else:
                    normalized = destination.replace("\\", "/").strip("/").casefold()
                    source_path = Path(source).expanduser()
                    source_name = source_path.name.casefold() if source_path.is_dir() else ""
                    first = normalized.split("/", 1)[0] if normalized else ""
                    filesystem = (
                        "frogfs"
                        if contiguous
                        or sentinel is not None
                        or first in {"bios", "fonts", "homebrews"}
                        or source_name in {"bios", "fonts", "homebrews"}
                        else "littlefs"
                    )
                if filesystem == "sd" and selected.resolved_sd is None:
                    raise ValueError(
                        "profile has no SD card image; create or assign one before adding SD content"
                    )
                return operate(
                    operation="add",
                    target="sd" if filesystem == "sd" else "flash/ext",
                    profile=profile,
                    filesystem=filesystem,
                    path=destination,
                    source=source,
                    contiguous=contiguous or sentinel is not None,
                    sentinel=sentinel,
                )
            raise ValueError("media add requires a device or profile target")
        if getattr(args, "abstract_media_files", False):
            target_path = getattr(args, "path", None) or "/"
            sdcard = bool(getattr(args, "sdcard", False))
            contiguous = bool(getattr(args, "contiguous", False))
            hinted = getattr(args, "filesystem", None)
            if sdcard and contiguous:
                raise ValueError("--sd/--sdcard cannot be combined with --contiguous")
            if device:
                target_text = str(device)
                blob_hint = None
                if target_text.startswith("blob:"):
                    target_text = target_text[5:]
                    if "=" in target_text:
                        blob_hint, target_text = target_text.split("=", 1)
                    elif target_text in {"frogfs", "littlefs", "sd", "sdcard"}:
                        blob_hint = target_text
                    if blob_hint in {"sd", "sdcard"}:
                        blob_hint = "sd"
                image_path = Path(target_text).expanduser() if target_text else None
                path_shaped = bool(
                    image_path
                    and (
                        "/" in target_text
                        or "\\" in target_text
                        or image_path.suffix.casefold() in {".bin", ".img", ".qcow2"}
                        or image_path.is_file()
                    )
                )
                if path_shaped:
                    from gwprov.filesystem import identify_filesystem_image

                    filesystem_hint = blob_hint or hinted
                    if filesystem_hint == "sdcard":
                        filesystem_hint = "sd"
                    detected = identify_filesystem_image(image_path, hint=filesystem_hint)
                    if contiguous and detected != "frogfs":
                        if detected == "sd":
                            raise ValueError("SD images cannot be combined with --contiguous")
                        if detected == "littlefs":
                            raise ValueError("--contiguous cannot be used with a LittleFS image")
                        raise ValueError("--contiguous requires a valid FrogFS image")
                    if sdcard and detected != "sd":
                        raise ValueError("--sd/--sdcard requires a valid FAT SD image")
                    filesystem = "sd" if detected == "sd" else detected
                    if action == "delete":
                        _confirm_apply(
                            args,
                            bool(getattr(args, "yes", False)),
                            [
                                f"Blob: {image_path}",
                                f"Filesystem: {filesystem}",
                                f"Delete: {target_path}",
                            ],
                        )
                    return operate(
                        operation=operation,
                        target="sd" if filesystem == "sd" else "flash/ext",
                        image=str(image_path),
                        filesystem=filesystem,
                        offset=getattr(args, "offset", 0),
                        size=image_path.stat().st_size,
                        block_size=getattr(args, "block_size", 4096),
                        path=target_path,
                    )
                if blob_hint is not None:
                    raise ValueError("blob:TYPE requires an existing filesystem image path")
                if (
                    "/" in target_text
                    or "\\" in target_text
                    or image_path.suffix.casefold() in {".bin", ".img", ".qcow2"}
                ):
                    raise ValueError(f"filesystem image does not exist: {image_path}")
            if profile:
                from gwprov.profiles import DeviceProfile

                selected_profile = DeviceProfile.load(profile)
                variant = _infer_project_variant_from_profile(selected_profile.root)
            elif device:
                row = _resolve_device_selector(device)
                assigned = row.get("assignedProfile")
                variant = None
                if assigned:
                    from gwprov.profiles import DeviceProfile

                    variant = _infer_project_variant_from_profile(DeviceProfile.load(assigned).root)
                if not variant:
                    description = (
                        str(row.get("application", "")) + " " + str(row.get("installed", ""))
                    ).casefold()
                    if "retro-go sd" in description:
                        variant = "sd"
                    elif "retro-go" in description:
                        variant = "flash"
                selected_profile = None
            else:
                raise ValueError("media operation requires a device or profile target")
            if variant == "sd" and contiguous:
                raise ValueError("--contiguous cannot be used with SD Retro-Go storage")
            if sdcard or variant == "sd":
                filesystem = "sd"
            elif contiguous:
                filesystem = "frogfs"
            else:
                path_root = target_path.replace("\\", "/").strip("/").casefold().split("/", 1)[0]
                filesystem = "frogfs" if path_root in {"bios", "fonts", "homebrews"} else "littlefs"
            if profile and filesystem == "sd" and selected_profile.resolved_sd is None:
                raise ValueError("profile has no SD card image")
            if device and action == "delete":
                _confirm_apply(
                    args,
                    bool(getattr(args, "yes", False)),
                    [
                        f"Target: {device}",
                        f"Storage: {'SD card' if filesystem == 'sd' else 'FrogFS' if filesystem == 'frogfs' else 'LittleFS'}",
                        f"Delete: {target_path}",
                    ],
                )
            if device:
                from gwprov.device_media import operate_device

                return operate_device(
                    device=device,
                    operation=operation,
                    filesystem=filesystem,
                    path=target_path,
                    yes=bool(getattr(args, "yes", False)),
                )
            return operate(
                operation=operation,
                target="sd" if filesystem == "sd" else "flash/ext",
                profile=profile,
                filesystem=filesystem,
                path=target_path,
                offset=getattr(args, "offset", 0),
                size=getattr(args, "size", None),
                block_size=getattr(args, "block_size", 4096),
            )
        filesystem = getattr(args, "filesystem", None)
        if filesystem not in {"frogfs", "littlefs", "sdcard"}:
            raise ValueError("choose a target filesystem: frogfs, littlefs, or sdcard")
        if filesystem == "sdcard":
            target = "sd"
            filesystem = "sd"
        else:
            target = "flash/ext"
        if image:
            from gwprov.filesystem import identify_filesystem_image

            detected = identify_filesystem_image(image)
            if detected != filesystem:
                raise ValueError(
                    f"selected filesystem {filesystem} does not match the validated {detected} image"
                )
        if profile:
            from gwprov.profiles import DeviceProfile

            selected_profile = DeviceProfile.load(profile)
            variant = _infer_project_variant_from_profile(selected_profile.root)
            if filesystem != "sd" and variant == "sd":
                raise ValueError("this profile uses SD Retro-Go; its content belongs on sdcard")
            if filesystem == "sd" and selected_profile.resolved_sd is None:
                raise ValueError("profile has no SD card image")
        if device:
            if operation in {"add", "delete"}:
                summary = (
                    [
                        f"Source: {Path(args.source).expanduser()}",
                        f"Destination: {filesystem}/{getattr(args, 'dest', '/')}",
                    ]
                    if operation == "add"
                    else [f"Target: {device}", f"Delete: {filesystem}/{args.path}"]
                )
                _confirm_apply(args, bool(getattr(args, "yes", False)), summary)
            from gwprov.device_media import operate_device

            return operate_device(
                device=device,
                operation=operation,
                filesystem=filesystem,
                path=getattr(args, "path", None) or getattr(args, "destination", None) or "/",
                source=getattr(args, "source", None),
            )
        if not profile and not image:
            raise ValueError("choose a profile or device target")
        return operate(
            operation=operation,
            target=target,
            profile=profile,
            image=image,
            filesystem=filesystem,
            offset=getattr(args, "offset", 0),
            size=getattr(args, "size", None),
            block_size=getattr(args, "block_size", 4096),
            path=(
                getattr(args, "dest", None)
                if operation == "add"
                else getattr(args, "path", None) or "/"
            ),
            source=getattr(args, "source", None),
            mapped_sources=getattr(args, "mapped_source", []),
            force=getattr(args, "force", False),
            contiguous=getattr(args, "contiguous", False),
            sentinel=getattr(args, "sentinel", None),
        )
    target = (
        "sd"
        if getattr(args, "target", "flash/ext") == "sdcard"
        else getattr(args, "target", "flash/ext")
    )
    size_mib = getattr(args, "size_mib", None)
    if action == "create":
        aliases = {
            "littlefs": "littlefs",
            "lfs": "littlefs",
            "frogfs": "frogfs",
            "sd": "sd",
            "sdcard": "sd",
        }
        filesystem = aliases.get(getattr(args, "fs_type", None), filesystem)
        if size_mib is None:
            size_mib = getattr(args, "size_mib_pos", None)
        if image is None and not args.profile:
            directory = Path(getattr(args, "target_dir", None) or ".").expanduser()
            filename = getattr(args, "filename", None)
            filename = filename or {
                "frogfs": "frogfs.bin",
                "littlefs": "lfs.bin",
                "sd": "sdcard.bin",
            }.get(filesystem)
            if not filename:
                raise ValueError("choose frogfs, littlefs/lfs, or sdcard/sd")
            image = str(directory / filename)
        if size is None and size_mib is not None and filesystem != "sd":
            size = size_mib * 1024 * 1024
    return operate(
        operation=operation,
        target=target,
        profile=profile,
        image=image,
        filesystem=filesystem,
        offset=args.offset,
        size=size,
        block_size=args.block_size,
        path=getattr(args, "destination", None) or getattr(args, "path", None) or "/",
        source=getattr(args, "source", None),
        mapped_sources=getattr(args, "mapped_source", []),
        size_mib=size_mib,
        force=getattr(args, "force", False),
    )


def _media_compare(args) -> int:
    from gwprov.inventory import compare

    result = compare(
        json.loads(Path(args.expected).read_text()),
        json.loads(Path(args.actual).read_text()),
        args.mode,
    )
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
                print(
                    f"  {version.get('tag', '?')}  {version.get('publishedAt', '')}"
                    + (f"  ({', '.join(suffixes)})" if suffixes else "")
                )
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
        result["targets"].append(
            {
                "id": target.get("id"),
                "kind": target.get("kind"),
                "label": target.get("label"),
                "systems": [
                    {
                        "id": system.get("id"),
                        "firmware": [
                            {
                                "id": slot.get("id"),
                                "filename": slot.get("filename"),
                                "required": slot.get("required", False),
                                "requiredFor": slot.get("requiredFor", []),
                            }
                            for slot in system.get("firmware", system.get("bios", []))
                        ],
                    }
                    for system in target.get("systems", [])
                ],
                "tools": [use.get("tool") for use in target.get("uses", [])],
            }
        )
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
            raise ValueError(
                f"cannot read profile provisioning metadata: {provision_path}: {error}"
            ) from error
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
    layout = provision.get("layout", {})
    if isinstance(layout, dict) and (layout.get("frogfsBytes") or layout.get("littlefsBytes")):
        return "flash"
    # The presence of an SD image is independent of firmware storage variant;
    # use the profile's filesystem layout or explicit firmware metadata only.
    return "flash"


def _retain_profile_mapped_sources(
    profile_root: Path, staged_frogfs: Path, ownership: dict, prior_projects: dict
) -> dict[str, str]:
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
                    raise ValueError(
                        f"mapped XiP source is missing from profile {profile_root.name}: {raw_source}"
                    )
            else:
                source = staged_frogfs / relative
                if not source.is_file():
                    source = None
                    from gwprov.profiles import DeviceProfile

                    profile = DeviceProfile.load(profile_root)
                    metadata_path = profile_root / "provision.json"
                    profile_metadata = (
                        json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
                    )
                    layout = profile_metadata.get("layout", {})
                    offset = int(layout.get("frogfsOffset", -1))
                    size = int(layout.get("frogfsBytes", 0))
                    raw_base = mapped.get("relocBase")
                    try:
                        reloc_base = (
                            int(raw_base, 0) if isinstance(raw_base, str) else int(raw_base)
                        )
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"mapped XiP path {relative} has no valid relocBase"
                        ) from exc
                    if offset < 0 or size <= 0 or not profile.extflash.is_file():
                        raise ValueError(
                            f"profile has no readable FrogFS image for mapped XiP path {relative}"
                        )
                    from gwprov.vendor.retrogo_sd.scripts.frogfs_pico8_ro import (
                        find_uncompressed_file_payload,
                        patch_mapped_bytes,
                    )

                    extflash = profile.extflash.read_bytes()
                    image = extflash[offset : offset + size]
                    found = find_uncompressed_file_payload(image, relative.as_posix())
                    if not found:
                        raise ValueError(
                            f"mapped XiP payload is missing or compressed in FrogFS: {relative}"
                        )
                    payload_offset, payload_size = found
                    target_address = 0x90000000 + offset + payload_offset
                    relocated = image[payload_offset : payload_offset + payload_size]
                    raw, _count = patch_mapped_bytes(relocated, reloc_base, target_address)
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
    profile_name = None
    variant_hint = None
    target_kind, target_value = (None, None)
    if args.target:
        target_kind, target_value = _resolve_profile_or_device_target(args.target)
    if args.output and target_kind:
        raise ValueError("choose either TARGET or --output, not both")
    if not args.output and not target_kind:
        raise ValueError(
            "project install requires profile:NAME, device:NAME, or --output DIRECTORY"
        )
    device_selector = target_value if target_kind == "device" else None
    profile_name = target_value if target_kind == "profile" else None
    if device_selector:
        from gwprov.device_assignments import get_assignment

        target_device = _resolve_device_selector(device_selector)
        device_id = target_device["id"]
        if target_device.get("kind") != "hardware":
            raise ValueError("project install to a device currently requires physical hardware")
        assignment = get_assignment(device_id)
        profile_name = assignment.get("profile")
        assigned_card_name = assignment.get("sdcard")
        if not profile_name:
            raise ValueError(
                f"device {device_id} has no assigned profile; apply one with `gwprov profile PROFILE apply device:{device_id}`"
            )
    if args.start and target_device is None:
        raise ValueError("--start applies only when TARGET is a physical device")
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
        output=output,
        variant=args.variant,
        versions_url=versions_url,
        variant_hint=variant_hint,
        target_id=args.project_target,
        tag=args.version,
        input_files=args.input,
        input_dirs=args.input_dir,
        firmware_files=args.firmware,
        firmware_dirs=args.firmware_dir,
        game_files=args.game,
        game_dirs=args.game_dir,
        bundle_path=bundle_path,
        dry_run=args.dry_run,
    )
    if args.format == "json":
        from contextlib import redirect_stdout

        with redirect_stdout(sys.stderr):
            result = install_project(name, **install_options)
    else:
        result = install_project(name, **install_options)
    variant = result["variant"]
    if profile_root is not None and result.get("requiresAbi"):
        provision_path = profile_root / "provision.json"
        profile_metadata = (
            json.loads(provision_path.read_text()) if provision_path.is_file() else {}
        )
        firmware = profile_metadata.get("firmware", {})
        firmware = firmware.get("firmware", {}) if isinstance(firmware, dict) else {}
        available = firmware.get("providesAbi") if isinstance(firmware, dict) else None
        required = result["requiresAbi"]
        if (
            not isinstance(required, dict)
            or not isinstance(available, dict)
            or available.get("version") != required.get("version")
            or available.get("size", 0) < required.get("minSize", 0)
        ):
            required_version = (
                required.get("version", "unknown") if isinstance(required, dict) else "unknown"
            )
            required_size = required.get("minSize", 0) if isinstance(required, dict) else 0
            raise ValueError(
                f"project requires Retro-Go ABI {required_version} "
                f"with at least {required_size} bytes; "
                "the profile's installed firmware ABI is missing or incompatible"
            )
    if args.start and (target_device is None or variant != "flash"):
        raise ValueError("--start applies only when TARGET is a physical device")
    if target_device is not None and variant == "sd":
        from gwprov.sd_cards import list_cards

        target_card = (
            next((item for item in list_cards() if item.get("name") == assigned_card_name), None)
            if assigned_card_name
            else None
        )
        if target_card is None:
            raise ValueError(f"device {target_device['id']} has no registered SD card assignment")
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
            destination = {
                "kind": "profile",
                "profile": profile_name,
                "path": str(profile_root.resolve()),
            }
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
                        item["deviceDestination"] = str(
                            Path(target_card["path"]) / Path(*rel.parts)
                        )
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
            payload_sizes = {
                item["path"].removeprefix("frogfs/"): item["bytes"]
                for item in planned
                if item["path"].startswith("frogfs/")
            }
            frogfs_offset = 16 * 1024 * 1024
            if profile_root is not None:
                provision_path = profile_root / "provision.json"
                profile_metadata = (
                    json.loads(provision_path.read_text()) if provision_path.is_file() else {}
                )
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
                        "pointerRange": (
                            f"[0x{base:08X}, 0x{base + item['payloadBytes']:08X}); Thumb bit ignored for range check and preserved in adjustment"
                            if item["payloadBytes"] is not None
                            else None
                        ),
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
                if variant == "flash"
                else "update the assigned profile SD image and overlay files onto the registered SD card"
            )
        if install_root is not None:
            install_root.cleanup()
        if args.format == "json":
            _print_structured(result, "json")
        else:
            from rich.table import Table
            from rich.text import Text

            console = Console()
            summary = Table(
                title="Project install plan", show_header=False, box=None, pad_edge=False
            )
            summary.add_column("Field", style="dim")
            summary.add_column("Value", overflow="fold")
            summary.add_row("Project", str(result.get("repo")))
            summary.add_row("Version", str(result.get("tag") or "Local bundle"))
            summary.add_row("Target", str(result.get("target", "unknown")))
            summary.add_row("Variant", str(variant))
            summary.add_row("Destination", str(destination.get("path")))
            if result.get("plannedOperation"):
                summary.add_row("Device action", result["plannedOperation"])
            if destination.get("device"):
                summary.add_row("Device", str(destination.get("deviceName")))
            if destination.get("sdCard"):
                summary.add_row("SD card", str(destination["sdCard"]["name"]))
                summary.add_row("SD card path", str(destination["sdCard"]["path"]))
            summary.add_row("Files", str(len(planned)))
            summary.add_row("Total size", _human_size(result["totalBytes"]))
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
                        f"0x{mapped['relocBase']:08X} to its FrogFS address in extflash."
                    )
                else:
                    console.print(
                        f"[cyan]Mapped XiP:[/] {mapped['path']} is stored on the SD card "
                        "and relocated by the SD loader."
                    )
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
            key: metadata
            for key, metadata in prior_projects_all.items()
            if isinstance(metadata, dict)
            and (metadata.get("project"), metadata.get("target")) in new_identities
        }
        prior_projects = {
            key: metadata
            for key, metadata in prior_projects_all.items()
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
            for metadata in ownership.values()
            if isinstance(metadata, dict)
            for path in metadata.get("files", [])
            if isinstance(path, str)
        )
        partitions = (
            ((staged_flash / "frogfs", "frogfs"), (staged_flash / "littlefs", "littlefs"))
            if variant == "flash"
            else ((staged / "sd", "fatfs"),)
        )
        if target_device is not None:
            staged_files = [
                file_path
                for root, _ in partitions
                if root.is_dir()
                for file_path in root.rglob("*")
                if file_path.is_file()
            ]
            writes = [
                {
                    "target": f"Profile {variant}: {file_path.relative_to(staged).as_posix()}",
                    "content": "Project file",
                    "bytes": file_path.stat().st_size,
                }
                for file_path in sorted(staged_files)
            ]
            _confirm_apply(
                args,
                bool(args.yes),
                [
                    f"Project: {result.get('repo')}",
                    f"Version: {result.get('tag')}",
                    f"Profile: {profile_root.name}",
                    f"Target: {target_device.get('name', target_device['id'])}",
                    (
                        "Action: update profile files and write its extflash regions"
                        if variant == "flash"
                        else "Action: update profile files and overlay its SD image onto the assigned card"
                    ),
                ],
                writes=writes,
            )
        mapped_sources = {}
        if variant == "flash":
            staged_frogfs = staged_flash / "frogfs"
            staged_frogfs_files = any(staged_frogfs.rglob("*")) if staged_frogfs.is_dir() else False
            has_new_mapped = any(
                project.get("mapped") for project in ownership.values() if isinstance(project, dict)
            )
            has_prior_mapped = any(
                project.get("mapped")
                for project in prior_projects.values()
                if isinstance(project, dict)
            )
            if has_new_mapped or (has_prior_mapped and staged_frogfs_files):
                mapped_sources = _retain_profile_mapped_sources(
                    profile_root, staged_frogfs, ownership, prior_projects
                )
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
            sidecars = (
                [f"{path}={source}" for path, source in mapped_sources.items()]
                if filesystem == "frogfs"
                else []
            )
            operate(
                operation="delete",
                target=target,
                profile=str(profile_root),
                filesystem=filesystem,
                path=relative,
                mapped_sources=sidecars,
            )

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
                sidecars = (
                    [
                        f"{path}={source}"
                        for path, source in mapped_sources.items()
                        if not (is_mapped_file and path == rel)
                    ]
                    if filesystem == "frogfs"
                    else []
                )
                operate(
                    operation="add",
                    target=target,
                    profile=str(profile_root),
                    filesystem=fs_option,
                    path=rel,
                    source=str(file_path),
                    mapped_sources=sidecars,
                )
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
                regions = [
                    row["region"] for row in plan["regions"] if row["region"] in selected_regions
                ]
                if not regions:
                    raise ValueError("profile has no deployable extflash project regions")
                device_id = target_device["id"]
                remote_url = (
                    device_id.removeprefix("remote:") if device_id.startswith("remote:") else None
                )
                progress, finish = _deployment_progress_display(args.format)
                try:
                    deployed = apply_deployment(
                        str(profile_root),
                        probe_id=target_device.get("probeId"),
                        remote_url=remote_url,
                        remote_origin=target_device.get("remoteOrigin"),
                        regions=regions,
                        start=args.start,
                        bank=args.bank,
                        progress=progress,
                    )
                finally:
                    if finish:
                        finish()
                result["deployment"] = deployed
            else:
                from gwprov.deploy import overlay_sd_tree

                count = overlay_sd_tree(staged / "sd", Path(target_card["path"]))
                result["sdcard"] = {
                    "name": target_card["name"],
                    "path": target_card["path"],
                    "filesWritten": count,
                    "mode": "overlay",
                }
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
        table.add_row("Project", str(result.get("repo")))
        table.add_row("Version", str(result.get("tag") or "Local bundle"))
        table.add_row("Target", str(result.get("target", "unknown")))
        table.add_row("Variant", str(result.get("variant", "unknown")))
        table.add_row("Files", str(len(result.get("files", []))))
        table.add_row("Installed to", str(result.get("profile") or args.output))
        if result.get("bundle"):
            table.add_row("Source", "Local bundle; publisher identity not authenticated")
        if result.get("deployment"):
            table.add_row(
                "Device", str(target_device.get("name", result.get("device", "write complete")))
            )
        if result.get("sdcard"):
            table.add_row("SD card", str(result["sdcard"]["name"]))
            table.add_row("Files overlaid", str(result["sdcard"]["filesWritten"]))
        Console().print(table)
    return 0


def _completion_bash(args) -> int:
    print(r"""_gwprov_profile_names() { gwprov profile list --output names 2>/dev/null; }
_gwprov_device_names() { gwprov device ls --completion 2>/dev/null; }
_gwprov_profile_targets() { while IFS= read -r n; do printf '%s\nprofile:%s\n' "$n" "$n"; done < <(_gwprov_profile_names); }
_gwprov_explicit_profile_targets() { while IFS= read -r n; do printf 'profile:%s\n' "$n"; done < <(_gwprov_profile_names); }
_gwprov_device_targets() { while IFS= read -r n; do printf '%s\ndevice:%s\n' "$n" "$n"; done < <(_gwprov_device_names); }
_gwprov_resource_targets() { _gwprov_explicit_profile_targets; _gwprov_device_targets; }
_gwprov_sdcard_names() { gwprov media sdcard list --output names 2>/dev/null; }
_gwprov_project_names() { gwprov project list --output json 2>/dev/null | python3 -c 'import json,sys; print("\n".join(p["project"] for p in json.load(sys.stdin).get("projects", [])))' 2>/dev/null; }
_gwprov_retro_releases() { gwprov retro-go list --output json 2>/dev/null | python3 -c 'import json,sys; print("\n".join(x["name"] for x in json.load(sys.stdin).get("releases", [])))' 2>/dev/null; }
_gwprov_complete_dirs() {
  local cur="$1" prefix=""
  compopt -o filenames 2>/dev/null || true
  if [[ "$cur" == //* && "$cur" != ///* ]]; then
    prefix="//"; cur="${cur#//}"
  fi
  while [[ "$cur" == *//* ]]; do cur="${cur//\/\//\/}"; done
  cur="$prefix$cur"
  mapfile -t COMPREPLY < <(compgen -f -- "$cur")
}
_gwprov_complete() {
  local cur="${COMP_WORDS[COMP_CWORD]}" prev="" command="${COMP_WORDS[1]-}" sub="${COMP_WORDS[2]-}" candidates
  (( COMP_CWORD > 0 )) && prev="${COMP_WORDS[COMP_CWORD-1]-}"
  case "$prev" in
    --profile) COMPREPLY=( $(compgen -W "$(_gwprov_profile_names)" -- "$cur") ); return ;;
    --probe-id) COMPREPLY=( $(compgen -W "$(_gwprov_device_names | grep '^probe:')" -- "$cur") ); return ;;
    --programmer) COMPREPLY=( $(compgen -W "stlink jlink cmsis-dap rpi-gpio" -- "$cur") ); return ;;
    --variant) COMPREPLY=( $(compgen -W "flash sd" -- "$cur") ); return ;;
    --format) COMPREPLY=( $(compgen -W "text json yaml html pdf" -- "$cur") ); return ;;
    --output|-o) COMPREPLY=( $(compgen -W "text json yaml names" -- "$cur") ); return ;;
    --region) COMPREPLY=( $(compgen -W "bank1 bank2 frogfs littlefs extflash sd" -- "$cur") ); return ;;
    --bank) COMPREPLY=( $(compgen -W "1 2" -- "$cur") ); return ;;
    --oc-level) COMPREPLY=( $(compgen -W "0 1 2 3" -- "$cur") ); return ;;
    --mode) COMPREPLY=( $(compgen -W "contents image" -- "$cur") ); return ;;
    --part) COMPREPLY=( $(compgen -W "bank1 bank2 bank0 extflash sdcard" -- "$cur") ); return ;;
    --retro-go|-r) COMPREPLY=( $(compgen -W "$(_gwprov_retro_releases)" -- "$cur") ); return ;;
    --filesystem) COMPREPLY=( $(compgen -W "frogfs littlefs lfs fatfs sdcard sd" -- "$cur") ); return ;;
  esac
  if [[ "$cur" == */* || "$cur" == *.zip || "$prev" == --gdb-endpoint || "$prev" == --input || "$prev" == --input-dir || "$prev" == --source || "$prev" == --image || "$prev" == --sdcard || "$prev" == --output-dir || "$prev" == --backup-dir || "$prev" == --retro-go-root ]]; then
    _gwprov_complete_dirs "$cur"; return
  fi
  if (( COMP_CWORD == 1 )); then candidates=$(gwprov tree --format names 2>/dev/null)
  elif (( COMP_CWORD == 2 )); then
    case "$command" in
      show) candidates="adapters devices projects profile media sdcard perf performance $(_gwprov_device_targets)" ;;
      adapters) candidates="list ls add remove rm" ;;
      start) candidates="$(_gwprov_device_targets) $(_gwprov_explicit_profile_targets)" ;;
      serve) candidates="$(_gwprov_device_targets)" ;;
      device) candidates="ls list $(_gwprov_device_targets)" ;;
      debug) candidates="python" ;;
      project) candidates="list ls versions info install stage-local" ;;
      backups) candidates="add list ls" ;;
      retro-go) candidates="list ls source install build config" ;;
      ofw) candidates="patch" ;;
      media) candidates="inventory compare frogfs littlefs create ls dir tree add delete sdcard sd" ;;
      report) candidates="render" ;;
      profile) candidates="create duplicate edit list ls show plan apply media $(_gwprov_profile_targets)" ;;
      tree) candidates="--format --no-pager" ;;
      completion) candidates="bash zsh" ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 3 )); then
    case "$command:$sub" in
      show:*) candidates="--output" ;;
      debug:python) candidates="$(_gwprov_device_targets)" ;;
      serve:*) candidates="start stop" ;;
      retro-go:config) candidates="$(_gwprov_resource_targets)" ;;
      profile:*)
        case "$sub" in
          list|ls) candidates="--output --no-pager" ;;
          create) candidates="--m --mario -m --zelda -z --patched -p --bootloader -b --retro-go -r --content --sdcard --no-sdcard --sdcard-size" ;;
          *) [[ " $(_gwprov_profile_names) " == *" $sub "* ]] && candidates="show edit duplicate plan apply media" ;;
        esac ;;
      device:*) if [[ "$sub" == ls || "$sub" == list ]]; then candidates="--output"; else candidates="stop recover pause halt resume reset logs screenshot duplicate perf diagnose watch input media"; fi ;;
      project:install|project:versions|project:info|project:list|project:ls) candidates="$(_gwprov_project_names)" ;;
      backups:add) _gwprov_complete_dirs "$cur"; return ;;
      ofw:patch) candidates="mario zelda" ;;
      media:create) candidates="frogfs littlefs lfs sdcard sd" ;;
      media:add|media:delete|media:ls|media:dir|media:tree) candidates="$(_gwprov_resource_targets)" ;;
      media:sdcard|media:sd) candidates="list ls add remove rm create compose" ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 4 )); then
    case "$command:$sub" in
      profile:*)
        case "${COMP_WORDS[3]-}" in
          apply) candidates="$(_gwprov_device_targets)" ;;
          edit) candidates="add remove --sdcard --no-sdcard --sdcard-size" ;;
          media) candidates="add delete ls tree" ;;
        esac ;;
      project:install) candidates="$(_gwprov_resource_targets)" ;;
      media:add) _gwprov_complete_dirs "$cur"; return ;;
      media:delete|media:ls|media:dir|media:tree) _gwprov_complete_dirs "$cur"; return ;;
      device:*) case "${COMP_WORDS[3]-}" in input) candidates="tap" ;; media) candidates="add delete ls dir tree" ;; *) candidates="" ;; esac ;;
      media:sdcard:remove|media:sd:remove) candidates="$(_gwprov_sdcard_names)" ;;
      media:sdcard:add|media:sd:add|media:sdcard:create|media:sd:create|media:sdcard:compose|media:sd:compose) _gwprov_complete_dirs "$cur"; return ;;
      *) candidates="" ;;
    esac
    if [[ "$command" == profile && "${COMP_WORDS[2]-}" != apply &&
          "${COMP_WORDS[3]-}" == apply ]]; then
      candidates="$(_gwprov_device_targets)"
    fi
  elif (( COMP_CWORD == 5 )); then
    if [[ "$command" == device && "${COMP_WORDS[3]-}" == media && "${COMP_WORDS[4]-}" == add ]] ||
       [[ "$command" == profile && "${COMP_WORDS[3]-}" == media && "${COMP_WORDS[4]-}" == add ]]; then
      _gwprov_complete_dirs "$cur"; return
    fi
  else candidates=""; fi
  COMPREPLY=( $(compgen -W "$candidates --help -h" -- "$cur") )
}
complete -F _gwprov_complete gwprov""")
    return 0


def _completion_zsh(args) -> int:
    print(r"""#compdef gwprov
_gwprov_profile_names() { gwprov profile list --output names 2>/dev/null }
_gwprov_device_names() { gwprov device ls --completion 2>/dev/null }
_gwprov_profile_targets() { while IFS= read -r n; do print -rl -- "$n" "profile:$n"; done < <(_gwprov_profile_names) }
_gwprov_explicit_profile_targets() { while IFS= read -r n; do print -r -- "profile:$n"; done < <(_gwprov_profile_names) }
_gwprov_device_targets() { while IFS= read -r n; do print -rl -- "$n" "device:$n"; done < <(_gwprov_device_names) }
_gwprov_resource_targets() { _gwprov_explicit_profile_targets; _gwprov_device_targets }
_gwprov_sdcard_names() { gwprov media sdcard list --output names 2>/dev/null }
_gwprov_project_names() { gwprov project list --output json 2>/dev/null | python3 -c 'import json,sys; print("\n".join(p["project"] for p in json.load(sys.stdin).get("projects", [])))' 2>/dev/null }
_gwprov() {
  local -a candidates
  local command=${words[2]} sub=${words[3]}
  case $words[CURRENT-1] in
    --profile) candidates=(${(@f)$(_gwprov_profile_names)}) ;;
    --probe-id) candidates=(${(@f)$(_gwprov_device_names | grep '^probe:')}) ;;
    --programmer) candidates=(stlink jlink cmsis-dap rpi-gpio) ;;
    --variant) candidates=(flash sd) ;;
    --format) candidates=(text json yaml html pdf) ;;
    --output|-o) candidates=(text json yaml names) ;;
    --region) candidates=(bank1 bank2 frogfs littlefs extflash sd) ;;
    --bank) candidates=(1 2) ;;
    --mode) candidates=(contents image) ;;
    --part) candidates=(bank1 bank2 bank0 extflash sdcard) ;;
    --gdb-endpoint|--sdcard|--sentinel|--dest|--dst|--destination|--input|--input-dir|--image|--output-dir|--backup-dir|--retro-go-root) _files; return ;;
    --filesystem) candidates=(frogfs littlefs lfs fatfs sdcard sd) ;;
    *)
      if [[ $words[CURRENT] == */* || $words[CURRENT] == *.zip ]]; then _files; return; fi
      if (( CURRENT == 3 )); then
        case $command in
          show) candidates=(adapters devices projects profile media sdcard perf performance ${(@f)$(_gwprov_device_targets)}) ;;
          adapters) candidates=(list ls add remove rm) ;;
          start) candidates=(${(@f)$(_gwprov_device_targets)} ${(@f)$(_gwprov_explicit_profile_targets)}) ;;
          serve) candidates=(${(@f)$(_gwprov_device_targets)}) ;;
          device) candidates=(ls list ${(@f)$(_gwprov_device_targets)}) ;;
          debug) candidates=(python) ;;
          project) candidates=(list ls versions info install stage-local) ;;
          backups) candidates=(add list ls) ;;
          retro-go) candidates=(list ls source install build config) ;;
          ofw) candidates=(patch) ;;
          media) candidates=(inventory compare frogfs littlefs create ls dir tree add delete sdcard sd) ;;
          report) candidates=(render) ;;
          profile) candidates=(create duplicate edit list ls show plan apply media ${(@f)$(_gwprov_profile_targets)}) ;;
          tree) candidates=(--format --no-pager) ;;
          completion) candidates=(bash zsh) ;;
        esac
      elif [[ $command == device ]]; then
        if (( CURRENT == 4 )); then
          if [[ $words[3] == ls || $words[3] == list ]]; then candidates=(--output)
          else candidates=(stop recover pause halt resume reset logs screenshot duplicate perf diagnose watch input media)
          fi
        elif (( CURRENT == 5 )); then
          case $words[4] in
            media) candidates=(add delete ls dir tree) ;;
            input) candidates=(tap) ;;
          esac
        elif (( CURRENT == 6 )) && [[ $words[4] == media && $words[5] == add ]]; then
          _files; return
        fi
      elif [[ $command == show && CURRENT == 4 ]]; then
        candidates=(--output)
      elif [[ $command == serve && CURRENT == 4 ]]; then
        candidates=(start stop)
      elif [[ $command == media ]]; then
        if (( CURRENT == 4 )); then
          case $sub in
            add|delete|ls|dir|tree) candidates=(${(@f)$(_gwprov_resource_targets)}) ;;
            create) candidates=(frogfs littlefs lfs sdcard sd) ;;
            sdcard|sd) candidates=(list ls add remove rm create compose) ;;
          esac
        elif (( CURRENT == 5 )) && [[ $sub == add ]]; then _files; return
        elif (( CURRENT == 5 )) && [[ $sub == delete || $sub == ls || $sub == dir || $sub == tree ]]; then _files; return
        fi
      elif [[ $command == profile ]]; then
        if (( CURRENT == 4 )); then
          case $sub in
            list|ls) candidates=(--output --no-pager) ;;
            apply|plan|show|edit|duplicate) candidates=() ;;
            *) [[ " ${(@j: :)$(_gwprov_profile_names)} " == *" ${words[3]} "* ]] && candidates=(show edit duplicate plan apply media) ;;
          esac
        elif (( CURRENT == 5 )); then
          if [[ $sub == apply || $words[4] == apply ]]; then candidates=(${(@f)$(_gwprov_device_targets)}); fi
          if [[ $sub == edit || $words[4] == edit ]]; then candidates=(add remove); fi
          if [[ $sub == media || $words[4] == media ]]; then candidates=(add delete ls dir tree); fi
        elif (( CURRENT >= 6 )) && [[ $words[CURRENT-2] == add ]]; then _files; return
        fi
      elif [[ $command == project ]]; then
        if (( CURRENT == 4 )) && [[ $sub == install || $sub == versions || $sub == info || $sub == list || $sub == ls ]]; then
          candidates=(${(@f)$(gwprov project list --output json 2>/dev/null | python3 -c 'import json,sys; print("\n".join(p["project"] for p in json.load(sys.stdin).get("projects", [])))' 2>/dev/null)})
        elif (( CURRENT == 5 )) && [[ $sub == install ]]; then candidates=(${(@f)$(_gwprov_resource_targets)})
        fi
      elif [[ $command == retro-go && $sub == config ]] && (( CURRENT == 4 )); then
        candidates=(${(@f)$(_gwprov_resource_targets)})
      elif [[ $command == debug && $sub == python ]] && (( CURRENT == 4 )); then
        candidates=(${(@f)$(_gwprov_device_targets)})
      fi
    ;;
  esac
  _describe 'values' candidates
}
compdef _gwprov gwprov""")
    return 0


def _command_tree(args) -> int:
    parser = build_parser()
    actions = [
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    ]
    if args.format == "names":
        choices = actions[0].choices if actions else {}
        canonical = {choice.dest for choice in actions[0]._choices_actions} if actions else set()
        ordered = [
            name
            for _, group in getattr(parser, "help_categories", ())
            for name in group
            if name in choices and name in canonical
        ]
        ordered_set = set(ordered)
        ordered.extend(name for name in choices if name not in ordered_set and name in canonical)
        for name in ordered:
            print(name)
        return 0

    root = Tree(Text("gwprov", style="bold bright_cyan"), guide_style="bright_black")

    def add_commands(
        current: argparse.ArgumentParser, parent: Tree, selected: list[str] | None = None
    ) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            canonical = {choice.dest for choice in action._choices_actions}
            names = (
                selected
                if selected is not None
                else [name for name in action.choices if name in canonical]
            )
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
    remaining = [
        name for name in top_level if name not in shown and id(top_level[name]) not in shown_parsers
    ]
    if remaining:
        category = root.add(Text("Other commands", style="bold bright_white"))
        add_commands(parser, category, remaining)

    guide = Group(
        root,
        Text(
            "Use `gwprov COMMAND --help` for options and examples.  "
            "Use `gwprov tree --format names` for script-friendly command names.",
            style="dim",
        ),
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
        epilog=(
            "Common starting points:\n\n"
            "  Overview              gwprov show\n"
            "  Start GWemu           gwprov start profile:PROFILE\n"
            "  Apply a profile       gwprov profile PROFILE apply device:DEVICE\n"
            "  Inspect a device      gwprov show DEVICE\n"
            "  Debug a device        gwprov debug python DEVICE\n"
            "  Add gnwmanager server gwprov adapters add NAME ws://HOST:PORT/gdb\n"
            "  Add raw GDB server    gwprov adapters add NAME gdb://HOST:PORT\n"
            "  Browse projects       gwprov project list\n"
            "  Browse all commands   gwprov tree\n\n"
            "Run `gwprov COMMAND --help` for command options and examples."
        ),
    )
    parser.help_categories = (
        ("Overview", ("show", "ps")),
        ("Adapters", ("adapters",)),
        ("Device controls", ("device", "start", "serve")),
        ("Profiles and storage", ("profile", "media")),
        ("Project catalog", ("project",)),
        ("Release and file workflows", ("retro-go", "ofw")),
        ("Debugging and reports", ("debug", "report")),
        ("Help and shell integration", ("tree", "completion", "daemon")),
    )
    commands = parser.add_subparsers(
        dest="command", required=True, metavar="COMMAND", parser_class=GWProvArgumentParser
    )
    daemon = commands.add_parser("daemon", help="inspect and restart the per-user service")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_restart = daemon_commands.add_parser("restart", help="drain and restart the daemon")
    daemon_restart.add_argument(
        "--force", action="store_true", help="cancel active substantial device operations"
    )
    daemon_restart.add_argument(
        "--timeout", type=float, default=30.0, help="seconds to wait for the restart (default: 30)"
    )
    daemon_restart.set_defaults(handler=_daemon_restart)
    show = commands.add_parser(
        "show",
        help="show a resource overview or details for one device",
        description=(
            "Show a concise overview, a resource category, or a named device.\n\n"
            "Examples:\n"
            "  gwprov show\n"
            "  gwprov show devices\n"
            "  gwprov show timing-test\n"
            "  gwprov show device:probe:SERIAL --output json"
        ),
    )
    show.add_argument(
        "section",
        nargs="?",
        metavar="CATEGORY_OR_DEVICE",
    )
    show.add_argument("--output", choices=("text", "json"), default="text")
    show.add_argument(
        "--firmware-repo", default="sylverb/game-and-watch-retro-go-sd", help=argparse.SUPPRESS
    )
    show.set_defaults(handler=_show)
    adapters = commands.add_parser("adapters", help="list and manage hardware adapters")
    adapters.set_defaults(handler=_adapters_list, output="text")
    adapter_commands = adapters.add_subparsers(dest="adapter_command")
    adapter_list = adapter_commands.add_parser(
        "list", aliases=("ls",), help="list local and registered adapters"
    )
    adapter_list.add_argument("--output", choices=("text", "json"), default="text")
    adapter_list.set_defaults(handler=_adapters_list)
    adapter_add = adapter_commands.add_parser("add", help="register a remote debug adapter")
    adapter_add.add_argument("name", help="short local name for this remote adapter")
    adapter_add.add_argument(
        "url",
        help="gnwmanager WebSocket URL (ws[s]://HOST:PORT/gdb) or raw GDB endpoint (gdb://HOST:PORT)",
    )
    adapter_add.add_argument(
        "--origin", help="Origin header required by a gnwmanager WebSocket server"
    )
    adapter_add.set_defaults(handler=_adapters_add)
    adapter_remove = adapter_commands.add_parser(
        "remove", aliases=("rm",), help="remove a registered remote adapter"
    )
    adapter_remove.add_argument("name_or_url", metavar="NAME_OR_URL")
    adapter_remove.set_defaults(handler=_adapters_remove)
    device = commands.add_parser(
        "device",
        help="inspect or control one named device",
        usage="gwprov device DEVICE ACTION ... | gwprov device ls [OPTIONS]",
        description=(
            "Every action names its device explicitly. Device actions do not use a saved selection.\n\n"
            "Examples:\n"
            "  gwprov device ls\n"
            "  gwprov device retro-go-vm pause\n"
            "  gwprov device device:probe:SERIAL media tree /CONFIG\n"
            "  gwprov device probe:SERIAL recover"
        ),
    )
    device_commands = device.add_subparsers(dest="device_command", required=True, metavar="ACTION")
    device_list = device_commands.add_parser(
        "ls", aliases=("list",), help="list GWemu and physical devices"
    )
    device_list.add_argument("--output", choices=("text", "json"), default="text")
    device_list.add_argument("--ids-only", action="store_true", help=argparse.SUPPRESS)
    device_list.add_argument("--completion", action="store_true", help=argparse.SUPPRESS)
    device_list.set_defaults(handler=_devices_command)
    start_profile = commands.add_parser(
        "start",
        help="start a device or launch a GWemu instance from a profile",
        description=(
            "Bare targets are devices. Use profile:NAME to launch a new managed GWemu instance.\n"
            "Starting a physical device is unsupported; use reset to reboot it."
        ),
    )
    start_profile.add_argument(
        "target", metavar="DEVICE|profile:PROFILE", help="device name, or profile:NAME/path"
    )
    start_profile.add_argument("--name", help="optional display name for the new GWemu device")
    start_profile.add_argument(
        "--gdb-port", type=int, help="GDB port; automatic allocation starts at 1235"
    )
    start_profile.add_argument("--audio", action="store_true")
    start_profile.add_argument("--headless", action="store_true")
    start_profile.add_argument("--timeline")
    start_profile.add_argument("--record-timeline")
    start_profile.add_argument(
        "--timing-mode", choices=("default", "baseline", "experimental-m7"), default="default"
    )
    start_profile.add_argument(
        "--icount-shift",
        "--icount",
        dest="icount",
        type=int,
        choices=range(11),
        help="precise instruction-count shift (legacy alias: --icount)",
    )
    start_profile.add_argument(
        "--pacing",
        choices=("realtime", "unthrottled"),
        help=(
            "host wall-clock pacing (baseline defaults to realtime; "
            "experimental-m7 requires unthrottled)"
        ),
    )
    start_profile.add_argument("--rtc-epoch", type=int)
    start_profile.add_argument("--gwemu-bin")
    start_profile.set_defaults(handler=_start_profile)
    serve = commands.add_parser(
        "serve",
        help="expose a device through the gnwmanager WebSocket protocol",
        usage="gwprov serve DEVICE {start,stop} [OPTIONS]",
        description=(
            "Start or stop a gnwmanager WebSocket bridge.\n"
            "Default: loopback, with port allocation starting at 8766.\n\n"
            "Examples:\n"
            "  gwprov serve device:gwemu:12345 start\n"
            "  gwprov serve device:probe:SERIAL start --port 8766\n"
            "  gwprov serve device:gwemu:12345 stop"
        ),
    )
    serve.add_argument("device", metavar="DEVICE", help="explicit device ID or configured name")
    serve_actions = serve.add_subparsers(dest="serve_action", required=True, metavar="ACTION")
    serve_start = serve_actions.add_parser("start", help="start a WebSocket bridge")
    serve_start.add_argument(
        "--bind", default="127.0.0.1", help="listen address (default: loopback)"
    )
    serve_start.add_argument(
        "--port", type=int, help="listen port (default: first free port from 8766)"
    )
    serve_start.add_argument(
        "--gdb-endpoint", help="select a GWemu listener: HOST:PORT, tcp://HOST:PORT, or unix:PATH"
    )
    serve_start.set_defaults(handler=_serve_device, action="start")
    serve_stop = serve_actions.add_parser("stop", help="stop the device's WebSocket bridge")
    serve_stop.set_defaults(handler=_serve_device, action="stop")
    stop_device = device_commands.add_parser(
        "stop", help="stop a managed GWemu device", usage="gwprov device DEVICE stop [OPTIONS]"
    )
    stop_device.add_argument("device", metavar="DEVICE")
    stop_device.add_argument("--timeout", type=float, default=10.0)
    stop_device.set_defaults(handler=_device_stop)
    recover = device_commands.add_parser(
        "recover",
        help="start or synchronize with a hardware recovery flasher",
        usage="gwprov device DEVICE recover",
    )
    recover.add_argument("device", metavar="DEVICE")
    recover.set_defaults(handler=_device_recover)
    for action, help_text in (
        ("pause", "pause a managed GWemu device"),
        ("halt", "halt a managed GWemu device"),
        ("resume", "resume a managed GWemu device"),
        ("reset", "reset a managed GWemu or hardware device"),
    ):
        control = device_commands.add_parser(
            action, help=help_text, usage=f"gwprov device DEVICE {action}"
        )
        control.add_argument("device", metavar="DEVICE")
        control.set_defaults(handler=_device_runtime_control, action=action)
    logs = device_commands.add_parser(
        "logs",
        help="show or follow a managed device's runtime log",
        usage="gwprov device DEVICE logs [OPTIONS]",
    )
    logs.add_argument("device", metavar="DEVICE")
    logs.add_argument(
        "--lines", type=int, default=80, help="number of trailing lines (default: 80)"
    )
    logs.add_argument(
        "--follow", "-f", action="store_true", help="follow until the managed process exits"
    )
    logs.set_defaults(handler=_device_logs)
    screenshot = device_commands.add_parser(
        "screenshot",
        help="capture a managed GWemu device screen",
        usage="gwprov device DEVICE screenshot --output FILE",
    )
    screenshot.add_argument("device", metavar="DEVICE")
    screenshot.add_argument("--output", required=True, help="PNG destination")
    screenshot.set_defaults(handler=_device_screenshot)
    duplicate_device = device_commands.add_parser(
        "duplicate",
        help="clone a device or copy one of its flash parts",
        usage="gwprov device DEVICE duplicate (--gwemu | --part PART) [OPTIONS]",
    )
    duplicate_device.add_argument("device", metavar="DEVICE")
    duplicate_kind = duplicate_device.add_mutually_exclusive_group(required=True)
    duplicate_kind.add_argument(
        "--gwemu",
        action="store_true",
        help="clone the complete managed profile into a new GWemu device",
    )
    duplicate_kind.add_argument(
        "--part",
        choices=("bank1", "bank2", "bank0", "extflash", "sdcard"),
        help="copy one profile flash part",
    )
    duplicate_device.add_argument("--name", help="new profile/device name for --gwemu")
    duplicate_device.add_argument("--output-dir", help="profile store for the clone")
    duplicate_device.add_argument("--output", help="destination file for --part")
    duplicate_device.add_argument("--headless", action="store_true")
    duplicate_device.add_argument("--gdb-port", type=int)
    duplicate_device.set_defaults(handler=_duplicate_device)
    ps = commands.add_parser("ps", help="show live GWemu and hardware state")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    ps.add_argument("--profile", help="load firmware/app symbols for physical Application state")
    ps.add_argument(
        "--live",
        "--refresh",
        dest="refresh",
        action="store_true",
        help="force a live runtime and internal-flash scan",
    )
    ps.set_defaults(handler=_ps)
    tree = commands.add_parser("tree", help="browse every command and what it does")
    tree.add_argument(
        "--format",
        choices=("tree", "names"),
        default="tree",
        help="show the command map or plain names for scripts",
    )
    tree.add_argument("--no-pager", action="store_true", help="write directly to the terminal")
    tree.set_defaults(handler=_command_tree)
    completion = commands.add_parser("completion", help="print a Bash or Zsh completion script")
    completion.add_argument("shell", choices=("bash", "zsh"))
    completion.set_defaults(
        handler=lambda args: (
            _completion_bash(args) if args.shell == "bash" else _completion_zsh(args)
        )
    )

    debug_tools = commands.add_parser("debug", help="attach a Python debugger to hardware or GWemu")
    debug_commands = debug_tools.add_subparsers(dest="debug_command", required=True)
    python_debug = debug_commands.add_parser(
        "python",
        help="attach a persistent Python console to a selected device",
        description=(
            "Attach to a named device or an explicitly selected adapter. Registered raw GDB "
            "servers connect directly; their target state is not polled before attach."
        ),
        epilog="Examples:\n  gwprov debug python DEVICE\n  gwprov debug python DEVICE --attach-only\n  gwprov debug python DEVICE --attach-only --no-watchdog-freeze\n  gwprov debug python DEVICE --autostart-timeout 20\n  gwprov debug python test-target\n  gwprov debug python probe:UNIQUE_ID\n  gwprov debug python 'GWemu pid 12345'\n  gwprov debug python --target hardware --probe-id UNIQUE_ID",
    )
    python_debug.add_argument(
        "device",
        nargs="?",
        help="device name or ID; required unless an explicit adapter is supplied",
    )
    python_debug.add_argument(
        "--target", choices=("gwemu", "hardware"), help="target kind when using an explicit adapter"
    )
    python_debug.add_argument("--host", default="127.0.0.1")
    python_debug.add_argument(
        "--port", type=int, help="GWemu GDB port; inferred for a managed instance"
    )
    python_debug.add_argument("--openocd-port", type=int, default=6666)
    python_debug.add_argument(
        "--probe-id",
        action="append",
        default=[],
        help="select a local PyOCD probe by unique ID (repeatable)",
    )
    python_debug.add_argument(
        "--programmer",
        action="append",
        default=[],
        choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
        help="start an explicit OpenOCD adapter session (repeatable by type)",
    )
    python_debug.add_argument(
        "--remote-url",
        action="append",
        default=[],
        help="connect through the daemon to one gnwmanager serve URL; repeat for independent servers",
    )
    python_debug.add_argument(
        "--remote-origin",
        action="append",
        default=[],
        help="Origin header for the corresponding remote URL (repeatable)",
    )
    python_debug.add_argument(
        "--qmp-socket",
        help="GWProv-managed QMP handle; normally discovered from the selected device",
    )
    python_debug.add_argument(
        "--debug-config", help="local ELF/map, relocation and counter descriptor"
    )
    python_debug.add_argument(
        "--profile", help="select a symbol profile when it is not inferred from the selected device"
    )
    python_debug.add_argument(
        "--symbols",
        action="append",
        default=[],
        metavar="ELF",
        help="load additional firmware, app, or project ELF symbols (repeatable)",
    )
    python_debug.add_argument(
        "--paused",
        action="store_true",
        help="set start_paused at the project's app_main autostart hook",
    )
    python_debug.add_argument(
        "--attach-only",
        action="store_true",
        help="open the Python console without resetting or waiting for app_main",
    )
    python_debug.add_argument(
        "--autostart-timeout",
        type=float,
        metavar="SECONDS",
        help="return to the Python console if automatic app_main launch takes this long",
    )
    python_debug.add_argument(
        "--no-watchdog-freeze",
        action="store_true",
        help="do not write MCU watchdog-freeze bits on attach",
    )
    python_debug.set_defaults(handler=_debug_python)

    project = commands.add_parser("project", help="inspect and stage GWRG-distributed projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_list = project_commands.add_parser(
        "list", aliases=("ls",), help="list curated cores and homebrew projects"
    )
    project_list.add_argument("name", nargs="?", help="optional project name or owner/name filter")
    project_list.add_argument(
        "--firmware-repo",
        default="sylverb/game-and-watch-retro-go-sd",
        help="firmware repository that publishes projects.json",
    )
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
        "install",
        help="verify a release or bundle ZIP and install it into content, a profile, or a device",
        description=(
            "Install a GWRG project from the network or an offline ZIP.\n"
            "TARGET is profile:NAME or device:NAME; use --output for a content directory.\n"
            "Variant is inferred from the destination or project release.\n"
            "ZIP hashes verify integrity, not publisher identity.\n"
            "See docs/PROFILES.md for the bundle format and examples.\n\n"
            "Examples:\n"
            "  gwprov project install doom profile:demo\n"
            "  gwprov project install ./doom.zip device:DEVICE --yes"
        ),
    )
    install.add_argument("repo", help="curated project, owner/repo, or path to a GWRG bundle ZIP")
    install.add_argument("--version", help="published tag; defaults to newest")
    install.add_argument(
        "--project-target",
        dest="project_target",
        help="manifest target id when a project publishes more than one target",
    )
    install.add_argument(
        "--variant", choices=("flash", "sd"), help="override inferred destination/release variant"
    )
    install.add_argument(
        "target",
        nargs="?",
        metavar="TARGET",
        help="profile:NAME or device:NAME; device writes to its assigned profile",
    )
    install_target = install.add_mutually_exclusive_group()
    install_target.add_argument("--output", help="staging root directory")
    install.add_argument(
        "--input",
        action="append",
        default=[],
        metavar="SLOT=FILE",
        help="converter input file; repeat for multiple files",
    )
    install.add_argument(
        "--input-dir",
        action="append",
        default=[],
        metavar="SLOT=DIR|DIR",
        help="non-recursive converter input directory; use DIR for one-slot converters or SLOT=DIR otherwise",
    )
    install.add_argument(
        "--firmware",
        "--bios",
        dest="firmware",
        action="append",
        default=[],
        metavar="ID=FILE",
        help="supply a firmware file (BIOS is an accepted synonym)",
    )
    install.add_argument(
        "--firmware-dir",
        "--bios-dir",
        dest="firmware_dir",
        action="append",
        default=[],
        metavar="DIR",
        help="find declared firmware files in a directory",
    )
    install.add_argument(
        "--game",
        action="append",
        default=[],
        metavar="SYSTEM=FILE",
        help="stage a game file and evaluate requiredFor firmware",
    )
    install.add_argument(
        "--game-dir",
        action="append",
        default=[],
        metavar="SYSTEM=DIR",
        help="stage files from a system game directory",
    )
    install.add_argument(
        "--dry-run",
        action="store_true",
        help="show detected variant, files and destinations without writing or contacting a device",
    )
    install.add_argument(
        "--format", choices=("text", "json"), default="text", help="result format (default: text)"
    )
    install.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="skip confirmation when installing directly to a device",
    )
    install.add_argument(
        "--start", action="store_true", help="start the selected bank after a flash project install"
    )
    install.add_argument(
        "--bank",
        type=int,
        choices=(1, 2),
        default=1,
        help="bank to start with --start (default: 1)",
    )
    install.set_defaults(handler=_project_install)
    stage_local = project_commands.add_parser(
        "stage-local", help="stage an unpublished local build into content"
    )
    stage_local.add_argument(
        "manifest", help="local JSON manifest describing sources and destinations"
    )
    stage_local.add_argument(
        "--output", required=True, help="content root populated by retro-go install"
    )
    stage_local.set_defaults(handler=_project_stage_local)

    backups = commands.add_parser(
        "backups", help="discover and list verified stock firmware backups"
    )
    backup_commands = backups.add_subparsers(dest="backup_action", required=True)
    backup_add = backup_commands.add_parser(
        "add", help="register a directory, ZIP, or complete backup file set"
    )
    backup_add.add_argument(
        "sources",
        nargs="+",
        metavar="DIRECTORY|FILE|ZIP",
        help="directory, ZIP, or both internal/external backup files",
    )
    backup_add.add_argument("--output", choices=("text", "json"), default="text")
    backup_add.set_defaults(handler=_backups)
    backup_list = backup_commands.add_parser(
        "list", aliases=("ls",), help="list discovered backup pairs"
    )
    backup_list.add_argument("--output", choices=("text", "json"), default="text")
    backup_list.set_defaults(handler=_backups)

    retro_go = commands.add_parser("retro-go", help="manage Retro-Go releases and file workflows")
    retro_commands = retro_go.add_subparsers(dest="retro_command", required=True)
    release_list = retro_commands.add_parser(
        "list", aliases=("ls",), help="list available releases from registered sources"
    )
    release_list.add_argument("source", nargs="?", help="limit to one registered source")
    release_list.add_argument("--sort", choices=("source", "version"), default="version")
    release_list.add_argument("--reverse", action="store_true")
    release_list.add_argument("--output", choices=("text", "json"), default="text")
    release_list.set_defaults(handler=_retro_release_list)
    retro_source = retro_commands.add_parser("source", help="manage release index sources")
    source_commands = retro_source.add_subparsers(dest="source_action", required=True)
    source_add = source_commands.add_parser(
        "add", help="register a GitHub repository or versions.json URL"
    )
    source_add.add_argument("source")
    source_add.set_defaults(handler=_retro_sources)
    source_remove = source_commands.add_parser("remove", help="remove a registered source")
    source_remove.add_argument("source")
    source_remove.set_defaults(handler=_retro_sources)
    source_list = source_commands.add_parser(
        "list", aliases=("ls",), help="list registered sources"
    )
    source_list.add_argument("--output", choices=("text", "json"), default="text")
    source_list.set_defaults(handler=_retro_sources)
    source_default = source_commands.add_parser("default", help="show or select the default source")
    source_default.add_argument("source", nargs="?")
    source_default.set_defaults(handler=_retro_sources)
    firmware_install = retro_commands.add_parser(
        "install", help="download verified release firmware and content"
    )
    firmware_install.add_argument("--repo", default="sylverb/game-and-watch-retro-go-sd")
    firmware_install.add_argument("--versions-url", help=argparse.SUPPRESS)
    firmware_install.add_argument("--version")
    firmware_install.add_argument("--variant", choices=("flash", "sd"), required=True)
    firmware_install.add_argument(
        "--output", required=True, help="content staging root shared with project install"
    )
    firmware_install.set_defaults(handler=_firmware_install)
    build = retro_commands.add_parser(
        "build",
        help="build Retro-Go release artifacts and stage them for profile creation",
        description=(
            "Build with Retro-Go's release targets, then\n"
            "stage firmware, ELF, and storage files with\n"
            "hashes. SD uses make release and sd_content.\n"
            "Flash uses all, frogfs_image, and\n"
            "littlefs_image. A profile sets SD_CARD;\n"
            "otherwise pass --variant.\n\n"
            "Examples:\n"
            "  gwprov retro-go build --profile retro-go-profile \\\n"
            "    --output build/retro-go\n"
            "  gwprov retro-go build --variant flash \\\n"
            "    --output build/retro-go-flash --dry-run"
        ),
    )
    build.add_argument(
        "--path", default="references/game-and-watch-retro-go-sd", help="Retro-Go source checkout"
    )
    build.add_argument("--profile", help="derive the storage variant from this existing profile")
    build.add_argument(
        "--variant", choices=("flash", "sd"), help="storage variant when no profile is selected"
    )
    build.add_argument("--output", required=True, help="new firmware staging directory")
    build.add_argument(
        "--extflash-part-mb", type=int, default=None, help="explicit extflash chip size"
    )
    build.add_argument(
        "--extflash-offset-mb", type=int, default=None, help="explicit extflash layout offset"
    )
    build.add_argument(
        "--jobs", type=int, default=None, help="parallel make jobs (default: CPU count)"
    )
    build.add_argument(
        "--makevar",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="additional release variable",
    )
    build.add_argument("--clean", action="store_true", help="clean before building")
    build.add_argument("--docker", action="store_true", help="build with Retro-Go's Docker target")
    build.add_argument("--quiet", action="store_true", help="hide build progress")
    build.add_argument(
        "--dry-run", action="store_true", help="print make commands without building"
    )
    build.set_defaults(handler=_build_retro_go)

    config = retro_commands.add_parser(
        "config",
        help="show or change Retro-Go /CONFIG for a device or profile",
        description=(
            "Read Retro-Go's /CONFIG, or change selected settings inline.\n"
            "TARGET is profile:NAME or device:NAME.\n\n"
            "Examples:\n"
            "  gwprov retro-go config device:retro-go-zelda\n"
            "  gwprov retro-go config device:retro-go-zelda --auto-start /homebrews/demo.bin --timeout 0\n"
            "  gwprov retro-go config profile:basic-test --oc-level 2"
        ),
    )
    config.add_argument("target", metavar="TARGET", help="profile:NAME or device:NAME")
    config.add_argument(
        "--output", metavar="FILE", help="also save the resulting 416-byte CONFIG blob"
    )
    config.add_argument(
        "--format", choices=("text", "json"), default="text", help="display format (default: text)"
    )
    auto_start = config.add_mutually_exclusive_group()
    auto_start.add_argument(
        "--auto-start", metavar="FILE_PATH", help="launch this Retro-Go file at boot"
    )
    auto_start.add_argument(
        "--no-auto-start",
        action="store_true",
        help="clear the automatic launch file and show the launcher",
    )
    config.add_argument(
        "--rom",
        nargs=2,
        metavar=("CORE", "FILENAME"),
        help="set auto-start from a core and filename, for example --rom gb Tetris.gb",
    )
    config.add_argument(
        "--timeout",
        "--menu-timeout",
        dest="timeout",
        type=int,
        help="main-menu idle timeout in seconds; use 0 to disable",
    )
    config.add_argument("--oc-level", type=int, choices=range(4))
    config.add_argument("--selected-tab", type=int)
    config.add_argument("--cursor", type=int)
    config.add_argument("--browse-subpath")
    config.add_argument(
        "-y",
        "--yes",
        "-f",
        "--force",
        action="store_true",
        help="skip confirmation before writing a device",
    )
    config.set_defaults(handler=_retro_go_config)

    perf = device_commands.add_parser(
        "perf",
        help="sample routine cycles from this device",
        usage="gwprov device DEVICE perf [OPTIONS]",
    )
    perf.add_argument("device", metavar="DEVICE")
    perf.add_argument("--profile", help="symbol profile override")
    perf.add_argument("--symbols", action="append", default=[], metavar="ELF")
    perf.add_argument("--counter-symbol", action="append", default=[], metavar="SYMBOL")
    perf.add_argument("--frame-counter")
    perf.add_argument("--total-cycle-counter")
    perf.add_argument("--start-at", metavar="SYMBOL")
    perf.add_argument("--set-register", action="append", default=[], metavar="REG=VALUE")
    perf.add_argument("--startup-timeout", type=float, default=120.0)
    perf.add_argument("--progress-symbol", action="append", metavar="SYMBOL")
    perf.add_argument("--progress-interval", type=float, default=1.0)
    perf.add_argument("--stall-threshold", type=float, default=1.0)
    perf.add_argument("--rebase", action="append", default=[], metavar="SECTION=POINTER_SYMBOL")
    perf.add_argument("--debug-config")
    perf.add_argument("--duration", type=float, default=15.0)
    perf.add_argument("--interval", type=float, default=0.02)
    perf.add_argument("--output")
    perf.add_argument("--format", choices=("text", "json"), default="text")
    perf.add_argument("--top", type=int, default=20)
    perf.set_defaults(
        handler=_device_perf, probe_id=None, programmer=None, remote_url=None, remote_origin=None
    )

    diagnose = device_commands.add_parser(
        "diagnose",
        help="capture a device screen and symbol-resolved call stack",
        usage="gwprov device DEVICE diagnose [OPTIONS]",
    )
    diagnose.add_argument("device", metavar="DEVICE")
    diagnose.add_argument("--symbols", action="append", default=[], metavar="ELF")
    diagnose.add_argument("--debug-config")
    diagnose.add_argument("--output", help="PNG path")
    diagnose.add_argument("--max-frames", type=int, default=32)
    diagnose.add_argument("--u32", action="append", default=[], metavar="SYMBOL")
    diagnose.add_argument("--deref", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE")
    diagnose.add_argument("--bytes", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE")
    diagnose.add_argument("--value", action="append", default=[], metavar="SYMBOL")
    diagnose.add_argument("--ring", action="append", default=[], metavar="ARRAY:HEAD")
    diagnose.set_defaults(handler=_device_diagnose)

    watch = device_commands.add_parser(
        "watch",
        help="watch this device and capture a report when it stalls",
        usage="gwprov device DEVICE watch [OPTIONS]",
    )
    watch.add_argument("device", metavar="DEVICE")
    watch.add_argument("--symbols", action="append", default=[], metavar="ELF")
    watch.add_argument("--debug-config")
    watch.add_argument("--progress-symbol", action="append", default=[], metavar="SYMBOL")
    watch.add_argument("--guest-pc-symbol", action="append", default=[], metavar="SYMBOL")
    watch.add_argument("--rebase", action="append", default=[], metavar="SECTION=POINTER_SYMBOL")
    watch.add_argument("--u32", action="append", default=[], metavar="SYMBOL")
    watch.add_argument("--deref", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE")
    watch.add_argument("--bytes", action="append", default=[], metavar="SYMBOL[+OFFSET]:SIZE")
    watch.add_argument("--value", action="append", default=[], metavar="SYMBOL")
    watch.add_argument("--ring", action="append", default=[], metavar="ARRAY:HEAD")
    watch.add_argument("--heartbeat-symbol", help=argparse.SUPPRESS)
    watch.add_argument("--frame-symbol", help=argparse.SUPPRESS)
    watch.add_argument("--interval", type=float, default=0.5)
    watch.add_argument("--stall-after", type=float, default=3.0)
    watch.add_argument("--duration", type=float, default=30.0)
    watch.add_argument("--output")
    watch.set_defaults(handler=_device_watch)

    ofw = commands.add_parser("ofw", help="prepare stock firmware images offline")
    ofw_commands = ofw.add_subparsers(dest="ofw_command", required=True)
    patch = ofw_commands.add_parser(
        "patch", help="build patched stock banks using gnwmanager's offline patch pipeline"
    )
    patch.add_argument("game", choices=("mario", "zelda"))
    patch.add_argument(
        "--backup-dir", required=True, help="directory of stock internal and external backups"
    )
    patch.add_argument("--output-dir", required=True)
    patch.add_argument("--extflash-mib", type=int, choices=(64, 128, 256), default=64)
    patch.add_argument("--no-bootloader", action="store_true")
    patch.set_defaults(handler=_ofw_patch)

    media = commands.add_parser(
        "media",
        help="inspect and edit profile or device filesystem contents",
        description=(
            "Flash Retro-Go uses FrogFS and LittleFS. SD Retro-Go keeps all content\n"
            "together on the SD card; NOR is a memory-mapped cache.\n\n"
            "Examples:\n"
            "  gwprov media add device:DEVICE ./bios --dest /\n"
            "  gwprov profile PROFILE media add ./game.bin --dest /homebrews\n"
            "  gwprov device DEVICE media add ./game.xip --sentinel 0xDEC00000\n"
            "  gwprov device DEVICE media tree /CONFIG\n"
            "  gwprov device DEVICE media add ./roms --sdcard --dest /"
        ),
    )
    media_commands = media.add_subparsers(dest="media_command", required=True)
    inventory = media_commands.add_parser(
        "inventory", help="read filesystem tables and hashes from a device profile"
    )
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
    comparison = media_commands.add_parser(
        "compare", help="compare saved filesystem inventories or image hashes"
    )
    comparison.add_argument("expected")
    comparison.add_argument("actual")
    comparison.add_argument("--mode", choices=("contents", "image"), default="contents")
    comparison.set_defaults(handler=_media_compare)
    for filesystem in ("frogfs", "littlefs"):
        pack = media_commands.add_parser(filesystem, help=f"run the vendored {filesystem} packer")
        pack.add_argument(
            "--retro-go-root",
            required=True,
            help="Retro-Go-SD checkout supplying tools, firmware constants, and sd_content",
        )
        pack.set_defaults(filesystem=filesystem)
        pack.set_defaults(handler=_fs_pack)

    def filesystem_target(operation, *, required=True):
        target = operation.add_mutually_exclusive_group(required=required)
        target.add_argument("-p", "--profile", help="named profile or profile path")
        target.add_argument("--image", help="explicit filesystem image")
        operation.add_argument(
            "--target", choices=("flash/ext", "sdcard", "sd"), default="flash/ext"
        )
        operation.add_argument("--filesystem", choices=("frogfs", "littlefs", "fatfs", "sd"))
        operation.add_argument("--offset", type=lambda value: int(value, 0), default=0)
        operation.add_argument("--size", type=lambda value: int(value, 0))
        operation.add_argument("--block-size", type=lambda value: int(value, 0), default=4096)
        operation.set_defaults(handler=_filesystem)

    def add_media_file_actions(parent, *, target_kind="option"):
        if isinstance(parent, argparse._SubParsersAction):
            file_actions = parent
        else:
            file_actions = parent.add_subparsers(
                dest="fs_command", required=True, metavar="{add,delete,ls,tree}"
            )

        add = file_actions.add_parser(
            "add",
            help="copy a local file or directory into a filesystem",
            usage=(
                "gwprov profile PROFILE media add [SOURCE]\n    [--dest DIRECTORY] [--contiguous] [--sentinel ADDRESS]"
                if target_kind == "profile"
                else (
                    "gwprov device DEVICE media add [SOURCE]\n    [--dest DIRECTORY] [--contiguous] [--sentinel ADDRESS]"
                    if target_kind == "device"
                    else "gwprov media add TARGET [SOURCE]\n    [--dest DIRECTORY] [--sdcard] [--contiguous] [--sentinel ADDRESS]"
                )
            ),
            description=(
                "Copy a file into its destination directory.\n"
                "Copy a directory beneath the destination using its source basename.\n"
                "Create missing parents. Storage is inferred from the profile/device and destination.\n"
                "On flash-only targets, /bios, /fonts, and /homebrews go to FrogFS; other paths go to LittleFS.\n"
                "Use --contiguous to store added files uncompressed in FrogFS."
            ),
        )
        if target_kind == "option":
            add.add_argument(
                "device",
                metavar="TARGET",
                help="device:NAME, profile:NAME, or standalone filesystem image",
            )
        add.add_argument(
            "source",
            nargs="?",
            default=".",
            help="local file or directory (default: current directory)",
        )
        add.add_argument(
            "--dest",
            "--dst",
            "--destination",
            default="/",
            metavar="DIRECTORY",
            help="destination directory inside the target filesystem (default: /)",
        )
        add.add_argument(
            "-c",
            "--contiguous",
            action="store_true",
            help="store files uncompressed and contiguously in FrogFS",
        )
        add.add_argument(
            "--sentinel",
            type=lambda value: int(value, 0),
            metavar="ADDRESS",
            help="relocate sentinel-based pointers to the file's FrogFS address",
        )
        add.add_argument(
            "--sd",
            "--sdcard",
            dest="sdcard",
            action="store_true",
            help="place content on the device/profile SD card",
        )
        if target_kind != "profile":
            add.add_argument(
                "-y",
                "--yes",
                "-f",
                "--force",
                action="store_true",
                help="skip confirmation and start the recovery flasher if needed",
            )
        add.set_defaults(abstract_media_add=True)
        add.add_argument(
            "--offset", type=lambda value: int(value, 0), default=0, help=argparse.SUPPRESS
        )
        add.add_argument("--size", type=lambda value: int(value, 0), help=argparse.SUPPRESS)
        add.add_argument(
            "--block-size", type=lambda value: int(value, 0), default=4096, help=argparse.SUPPRESS
        )
        add.add_argument(
            "--mapped-source",
            action="append",
            default=[],
            metavar="PATH=FILE",
            help=argparse.SUPPRESS,
        )
        add.set_defaults(handler=_filesystem)

        delete = file_actions.add_parser(
            "delete",
            usage=(
                "gwprov profile PROFILE media delete PATH [--sdcard] [--contiguous]"
                if target_kind == "profile"
                else (
                    "gwprov device DEVICE media delete PATH [--sdcard] [--contiguous]"
                    if target_kind == "device"
                    else "gwprov media delete TARGET PATH [--sdcard] [--contiguous]"
                )
            ),
            help="delete a file from inferred media storage",
        )
        if target_kind == "option":
            delete.add_argument(
                "device",
                metavar="TARGET_OR_BLOB",
                help="device:NAME, profile:NAME, or filesystem image path",
            )
        delete.add_argument("path", help="file path inside the target filesystem")
        delete.add_argument(
            "--filesystem", choices=("frogfs", "littlefs", "sdcard"), help=argparse.SUPPRESS
        )
        delete.add_argument(
            "--sd", "--sdcard", dest="sdcard", action="store_true", help="use SD-card storage"
        )
        delete.add_argument("-c", "--contiguous", action="store_true", help="select FrogFS storage")
        delete.add_argument(
            "--offset", type=lambda value: int(value, 0), default=0, help=argparse.SUPPRESS
        )
        delete.add_argument("--size", type=lambda value: int(value, 0), help=argparse.SUPPRESS)
        delete.add_argument(
            "--block-size", type=lambda value: int(value, 0), default=4096, help=argparse.SUPPRESS
        )
        delete.add_argument(
            "--mapped-source",
            action="append",
            default=[],
            metavar="PATH=FILE",
            help=argparse.SUPPRESS,
        )
        if target_kind != "profile":
            delete.add_argument(
                "-y",
                "--yes",
                "-f",
                "--force",
                action="store_true",
                help="skip confirmation before changing a device",
            )
        delete.set_defaults(handler=_filesystem, abstract_media_files=True)

        for action, aliases in (("ls", ("dir",)), ("tree", ())):
            listing = file_actions.add_parser(
                action,
                aliases=aliases,
                usage=(
                    f"gwprov profile PROFILE media {action} [PATH] [--sdcard] [--contiguous]"
                    if target_kind == "profile"
                    else (
                        f"gwprov device DEVICE media {action} [PATH] [--sdcard] [--contiguous]"
                        if target_kind == "device"
                        else f"gwprov media {action} TARGET [PATH] [--sdcard] [--contiguous]"
                    )
                ),
                help="list a path in inferred media storage",
            )
            if target_kind == "option":
                listing.add_argument(
                    "device",
                    metavar="TARGET_OR_BLOB",
                    help="device:NAME, profile:NAME, or filesystem image path",
                )
            listing.add_argument(
                "--filesystem", choices=("frogfs", "littlefs", "sdcard"), help=argparse.SUPPRESS
            )
            listing.add_argument(
                "--sd", "--sdcard", dest="sdcard", action="store_true", help="use SD-card storage"
            )
            listing.add_argument(
                "-c", "--contiguous", action="store_true", help="select FrogFS storage"
            )
            listing.add_argument("path", nargs="?", help="file or directory inside the filesystem")
            listing.add_argument(
                "--offset", type=lambda value: int(value, 0), default=0, help=argparse.SUPPRESS
            )
            listing.add_argument("--size", type=lambda value: int(value, 0), help=argparse.SUPPRESS)
            listing.add_argument(
                "--block-size",
                type=lambda value: int(value, 0),
                default=4096,
                help=argparse.SUPPRESS,
            )
            if target_kind != "profile":
                listing.add_argument(
                    "-y",
                    "--yes",
                    "-f",
                    "--force",
                    action="store_true",
                    help="skip confirmation before changing a device",
                )
            listing.set_defaults(handler=_filesystem, abstract_media_files=True)

    create_fs = media_commands.add_parser(
        "create",
        help="create or format a filesystem",
        usage="gwprov media create TYPE N_MIB [TARGET_DIR] [FILENAME] [OPTIONS]",
        description=(
            "Create a FrogFS, LittleFS, or partitioned SD image. N_MIB is the capacity in MiB. "
            "TARGET_DIR defaults to the current directory; FILENAME defaults to frogfs.bin, "
            "lfs.bin, or sdcard.bin for the selected type. Existing files require --force."
        ),
    )
    filesystem_target(create_fs, required=False)
    create_fs.add_argument(
        "fs_type",
        nargs="?",
        choices=("frogfs", "littlefs", "lfs", "sdcard", "sd"),
        help="filesystem type",
    )
    create_fs.add_argument(
        "size_mib_pos",
        nargs="?",
        type=int,
        metavar="N_MIB",
        help="filesystem/image capacity in MiB",
    )
    create_fs.add_argument(
        "target_dir", nargs="?", help="output directory (default: current directory)"
    )
    create_fs.add_argument(
        "filename", nargs="?", help="output filename (defaults by filesystem type)"
    )
    create_fs.add_argument(
        "--size-mib", type=int, help="SD image capacity or profile SD size in MiB"
    )
    create_fs.add_argument("--force", action="store_true", help="replace existing filesystem data")
    create_fs.set_defaults(handler=_filesystem, fs_command="create")
    add_media_file_actions(media_commands)

    media_sdcard = media_commands.add_parser(
        "sdcard", aliases=("sd",), help="manage mounted SD cards and SD images"
    )
    media_sdcard_commands = media_sdcard.add_subparsers(dest="sdcard_command", required=True)
    media_sdcard_list = media_sdcard_commands.add_parser(
        "list", aliases=("ls",), help="list registered SD cards"
    )
    media_sdcard_list.add_argument("--output", choices=("text", "json", "names"), default="text")
    media_sdcard_list.set_defaults(handler=_sdcard)
    media_sdcard_add = media_sdcard_commands.add_parser(
        "add", help="register an already-mounted SD card folder or drive"
    )
    media_sdcard_add.add_argument("path", help="mounted folder or drive path")
    media_sdcard_add.add_argument(
        "name", nargs="?", help="card name; defaults to folder name or drive letter"
    )
    media_sdcard_add.set_defaults(handler=_sdcard)
    media_sdcard_remove = media_sdcard_commands.add_parser(
        "remove", aliases=("rm",), help="unregister an SD card"
    )
    media_sdcard_remove.add_argument("name")
    media_sdcard_remove.set_defaults(handler=_sdcard)
    media_sdcard_create = media_sdcard_commands.add_parser(
        "create", help="create a partitioned FAT32 SD image"
    )
    media_sdcard_create.add_argument("image")
    media_sdcard_create.add_argument("--size-mb", type=int, required=True)
    media_sdcard_create.add_argument("--label", default="RETROGO")
    media_sdcard_create.set_defaults(handler=_sd_create)
    media_sdcard_compose = media_sdcard_commands.add_parser(
        "compose", help="copy selected Retro-Go content onto an image"
    )
    media_sdcard_compose.add_argument("image")
    media_sdcard_compose.add_argument("--content-dir")
    media_sdcard_compose.add_argument("--core")
    media_sdcard_compose.add_argument("--core-name", default="dos")
    media_sdcard_compose.add_argument("--rom", action="append", default=[])
    media_sdcard_compose.add_argument("--rom-dir", default="dos")
    media_sdcard_compose.add_argument("--config")
    media_sdcard_compose.set_defaults(handler=_sd_compose)

    input_group = device_commands.add_parser(
        "input",
        help="send controller input to this target",
        usage="gwprov device DEVICE input tap BUTTON",
    )
    input_group.add_argument("device", metavar="DEVICE")
    input_commands = input_group.add_subparsers(dest="input_command", required=True)
    tap = input_commands.add_parser(
        "tap",
        help="tap a button or chord over the probe",
        usage="gwprov device DEVICE input tap BUTTON [OPTIONS]",
    )
    tap.add_argument("buttons", help="button name or chord, such as LEFT+GAME")
    tap.add_argument("--repeat", type=int, default=1)
    tap.add_argument("--tap-ms", type=int, default=80)
    tap.add_argument("--gap-ms", type=int, default=120)
    tap.set_defaults(handler=_input_tap)

    reports = commands.add_parser("report", help="render structured reports for review")
    report_commands = reports.add_subparsers(dest="report_command", required=True)
    render = report_commands.add_parser("render", help="render JSON as portable HTML or PDF")
    render.add_argument("--input", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--format", choices=("html", "pdf"), required=True)
    render.set_defaults(handler=_report_render)

    profile = commands.add_parser(
        "profile",
        help="create and manage device profiles",
        usage="gwprov profile PROFILE ACTION ... | gwprov profile {create,list,ls} ...",
        description=(
            "Manage profiles by naming the profile before its action. "
            "Creation and collection listing start with their verb.\n\n"
            "Examples:\n"
            "  gwprov profile create mario-stock -m\n"
            "  gwprov profile mario-stock plan\n"
            "  gwprov profile mario-stock apply device:probe:SERIAL\n"
            "  gwprov profile list"
        ),
    )
    profile_commands = profile.add_subparsers(
        dest="profile_command",
        required=True,
        metavar="{create,duplicate,edit,list,ls,show,plan,apply,media}",
    )
    create = profile_commands.add_parser(
        "create", help="create a device profile from content or stock media"
    )
    create.add_argument(
        "directory", metavar="NAME_OR_PATH", help="managed profile name, or an explicit output path"
    )
    create.add_argument(
        "--output-dir",
        help="profile store for this creation (default: GWPROV_PROFILE_DIR or XDG data home)",
    )
    model_group = create.add_mutually_exclusive_group()
    model_group.add_argument(
        "-m", "--mario", action="store_true", help="include Mario stock firmware and assets"
    )
    model_group.add_argument(
        "-z", "--zelda", action="store_true", help="include Zelda stock firmware and assets"
    )
    create.add_argument(
        "-p", "--patched", action="store_true", help="apply the standard stock firmware patch"
    )
    create.add_argument(
        "-b",
        "--bootloader",
        action="store_true",
        help="include the SD bootloader used by Game+Left at 0x08032000",
    )
    create.add_argument(
        "--content", help="staged content root populated by Retro-Go/project install"
    )
    create.add_argument(
        "-r",
        "--retro-go",
        metavar="SOURCE:VERSION",
        help="add an exact release listed by `gwprov retro-go list`",
    )
    create.add_argument("--name")
    create.add_argument(
        "--littlefs", "--littlefs-mib", dest="littlefs_mib", type=int, default=2, metavar="SIZE_MB"
    )
    create_sd = create.add_mutually_exclusive_group()
    create_sd.add_argument(
        "--sdcard",
        metavar="IMAGE",
        help="include a bundled SD image, copying this image when supplied",
    )
    create_sd.add_argument(
        "--no-sdcard",
        action="store_true",
        help="omit a bundled SD image (the default for flash profiles)",
    )
    create.add_argument(
        "--sdcard-size",
        type=int,
        metavar="SIZE_MB",
        help="create a bundled SD image of this size; implies --sdcard",
    )
    create.add_argument(
        "--sd-label", default="RETROGO", help="volume label for the bundled SD image"
    )
    create.add_argument(
        "--extflash-mib",
        type=int,
        choices=(64, 128, 256),
        help="chip capacity; flash defaults to the smallest size that fits, SD defaults to 64 MiB",
    )
    create.add_argument("--bootloader-repo", default="sylverb/game-and-watch-bootloader")
    create.add_argument("--bootloader-version", default="v1.0.8", help="release tag or latest")
    create.add_argument("--bootloader-file", help="use a local binary linked at 0x08000000")
    create.add_argument(
        "--sd-bootloader-file", help="use a local SD bootloader linked at 0x08032000 (requires -b)"
    )
    create.set_defaults(handler=_profile_create)
    duplicate = profile_commands.add_parser(
        "duplicate",
        help="copy a profile into an independent working profile",
        usage="gwprov profile PROFILE duplicate DESTINATION [OPTIONS]",
    )
    duplicate.add_argument(
        "source", metavar="PROFILE", help="managed profile name or explicit profile path"
    )
    duplicate.add_argument(
        "destination",
        metavar="DESTINATION",
        help="new managed profile name or explicit output path",
    )
    duplicate.add_argument(
        "--output-dir",
        help="profile store for the copy (default: GWPROV_PROFILE_DIR or platform data directory)",
    )
    duplicate.add_argument("--output", choices=("text", "json"), default="text")
    duplicate.set_defaults(handler=_profile_duplicate)
    edit = profile_commands.add_parser(
        "edit",
        help="change a profile's mapped flash components or SD card",
        usage="gwprov profile PROFILE edit [OPTIONS] [add|remove ...]",
    )
    edit.add_argument("profile", metavar="PROFILE", help="managed profile name or profile path")
    edit_sd = edit.add_mutually_exclusive_group()
    edit_sd.add_argument(
        "--sdcard", metavar="IMAGE", help="include or replace the bundled SD image"
    )
    edit_sd.add_argument("--no-sdcard", action="store_true", help="remove the bundled SD image")
    edit.add_argument(
        "--sdcard-size",
        type=int,
        metavar="SIZE_MB",
        help="create a bundled SD image of this size; implies --sdcard",
    )
    edit.add_argument("-o", "--output", choices=("text", "json"), default="text")
    edit_commands = edit.add_subparsers(dest="action", required=False, metavar="{add,remove}")
    blob_add = edit_commands.add_parser("add", help="add or replace one component")
    blob_add.add_argument(
        "bank",
        choices=("bank1", "bank2", "bank0", "extflash"),
        help="internal bank1/bank2 or external flash bank0/extflash",
    )
    blob_add.add_argument("name", help="display name used in apply summaries")
    blob_add.add_argument("file", help="source blob file")
    blob_add.add_argument(
        "--offset",
        required=True,
        type=lambda value: int(value, 0),
        help="offset within the selected bank (decimal or 0x-prefixed)",
    )
    blob_add.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing blob with the same name in that bank",
    )
    blob_add.add_argument("-o", "--output", choices=("text", "json"), default="text")
    blob_add.set_defaults(handler=_profile_blob, action="add")
    blob_remove = edit_commands.add_parser("remove", help="remove a mapped component")
    blob_remove.add_argument("bank", choices=("bank1", "bank2", "bank0", "extflash"))
    blob_remove.add_argument("name", help="display name of the component to remove")
    blob_remove.add_argument("-o", "--output", choices=("text", "json"), default="text")
    blob_remove.set_defaults(handler=_profile_blob, action="remove")
    edit.set_defaults(handler=_profile_edit, action=None)
    profile_plan = profile_commands.add_parser(
        "plan",
        help="show exact bank and filesystem writes without touching a device",
        usage="gwprov profile PROFILE plan [OPTIONS]",
    )
    profile_plan.add_argument("profile_name", metavar="PROFILE")
    profile_plan.add_argument(
        "--region",
        action="append",
        choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"),
    )
    profile_plan.add_argument("-o", "--output", choices=("text", "json", "yaml"), default="text")
    profile_plan.set_defaults(handler=_profile_plan)
    profile_apply = profile_commands.add_parser(
        "apply",
        help="write this profile to a named hardware device",
        description="Apply one profile directly to one physical device.",
        usage="gwprov profile PROFILE apply device:DEVICE [OPTIONS]",
    )
    profile_apply.add_argument("profile_name", metavar="PROFILE")
    profile_apply.add_argument("device", metavar="DEVICE", help="hardware device name or stable ID")
    profile_apply.add_argument(
        "--region",
        action="append",
        choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"),
        help="region to deploy; repeatable, defaults to all profile regions",
    )
    profile_apply.add_argument("--start", action="store_true", help="start firmware after writing")
    profile_apply.add_argument(
        "--bank",
        type=int,
        choices=(1, 2),
        default=1,
        help="bank to start with --start (default: 1)",
    )
    profile_apply.add_argument("-o", "--output", choices=("text", "json", "yaml"), default="text")
    profile_apply.add_argument(
        "-y", "--yes", "-f", "--force", action="store_true", help="skip the write confirmation"
    )
    profile_apply.set_defaults(
        handler=_profile_apply, probe_id=None, programmer=None, remote_url=None, remote_origin=None
    )
    profile_list = profile_commands.add_parser(
        "list", aliases=("ls",), help="list profiles in the managed profile store"
    )
    profile_list.add_argument(
        "--output",
        choices=("text", "json", "names"),
        default="text",
        help="table, JSON, or plain names for shell completion",
    )
    profile_list.add_argument(
        "--no-pager", action="store_true", help="write directly to the terminal"
    )
    profile_list.set_defaults(handler=_profile_list)
    show = profile_commands.add_parser(
        "show",
        help="inspect a named or path-based device profile",
        usage="gwprov profile PROFILE show [OPTIONS]",
    )
    show.add_argument("directory", metavar="NAME_OR_PATH")
    show.add_argument("--shared-sd-root")
    show.set_defaults(handler=_profile_show)

    profile_media = profile_commands.add_parser(
        "media",
        help="inspect or edit profile filesystem contents",
        usage="gwprov profile PROFILE media {add,delete,ls,tree} ...",
    )
    profile_media.add_argument(
        "profile", metavar="PROFILE", help="managed profile name or profile path"
    )
    add_media_file_actions(profile_media, target_kind="profile")

    device_media = device_commands.add_parser(
        "media",
        help="inspect or edit a device filesystem",
        usage="gwprov device DEVICE media {add,delete,ls,tree} ...",
    )
    device_media.add_argument(
        "device", metavar="DEVICE", help="device name or stable ID; optional device: prefix"
    )
    add_media_file_actions(device_media, target_kind="device")
    return parser


def _normalize_profile_object_form(argv: list[str] | None) -> list[str] | None:
    """Normalize the public object-first profile form for argparse."""
    if argv is None:
        return None
    if len(argv) < 3 or argv[0] not in {"profile"}:
        return argv
    known_actions = {"create", "duplicate", "edit", "list", "ls", "show", "plan", "apply", "media"}
    resource_actions = {"duplicate", "edit", "show", "plan", "apply", "media"}
    if argv[1] == "create":
        return argv
    if argv[2] in resource_actions:
        return ["profile", argv[2], argv[1], *argv[3:]]
    if argv[1] in {"create", "list", "ls"} or argv[1].startswith("-"):
        return argv
    if argv[1] in known_actions:
        raise ValueError(
            f"Use `gwprov profile PROFILE {argv[1]}`; profile actions follow the profile name"
        )
    return argv


def _normalize_device_object_form(argv: list[str] | None) -> list[str] | None:
    """Normalize ``device NAME ACTION`` while keeping inventory as ``device ls``."""
    if not argv or len(argv) < 2 or argv[0] != "device":
        return argv
    actions = {
        "stop",
        "recover",
        "pause",
        "halt",
        "resume",
        "reset",
        "logs",
        "screenshot",
        "duplicate",
        "perf",
        "diagnose",
        "watch",
        "input",
        "media",
    }
    if argv[1] in {"ls", "list"}:
        return argv
    if argv[1] in actions:
        raise ValueError(
            f"Use `gwprov device DEVICE {argv[1]}`; device actions follow the device name"
        )
    if len(argv) >= 3 and argv[2] in actions:
        return ["device", argv[2], argv[1], *argv[3:]]
    if len(argv) == 2 and argv[1] in {"--help", "-h"}:
        return argv
    if len(argv) == 2 and not argv[1].startswith("-"):
        raise ValueError(f"choose an action for device {argv[1]!r}; run `gwprov device --help`")
    return argv


def _normalize_media_blob_form(argv: list[str] | None) -> list[str] | None:
    """Accept standalone image targets before media actions."""
    if not argv or len(argv) < 3 or argv[0] != "media":
        return argv
    actions = {"add", "delete", "ls", "dir", "tree"}
    target = argv[1]
    if target in {"inventory", "compare", "frogfs", "littlefs", "create", "sdcard", "sd"}:
        return argv
    typed_blob = target.startswith("blob:")
    path_like = (
        typed_blob
        or "/" in target
        or "\\" in target
        or target.startswith("~")
        or Path(target).suffix.casefold() in {".bin", ".img", ".qcow2"}
        or Path(target).expanduser().is_file()
    )
    if not path_like:
        return argv
    if argv[2] in actions:
        action, tail = argv[2], argv[3:]
        image_spec = target
    elif len(argv) >= 4 and target.startswith("blob:") and argv[3] in actions:
        action, tail = argv[3], argv[4:]
        image_spec = target + "=" + argv[2]
    else:
        return argv
    if action == "add":
        return ["media", "add", image_spec, *tail]
    from gwprov.filesystem import identify_filesystem_image

    hint = None
    image_path = image_spec
    if image_spec.startswith("blob:"):
        spec = image_spec[5:]
        if "=" in spec:
            hint, image_path = spec.split("=", 1)
            hint = "sd" if hint in {"sd", "sdcard"} else hint
        else:
            hint = spec if spec in {"frogfs", "littlefs", "sd", "sdcard"} else None
            image_path = spec if hint else spec
    detected = identify_filesystem_image(image_path, hint=hint)
    selector = "sdcard" if detected == "sd" else detected
    return ["media", action, image_path, *tail, "--filesystem", selector]


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = argv if argv is not None else sys.argv[1:]
    try:
        argv = _normalize_profile_object_form(argv)
        argv = _normalize_device_object_form(argv)
        argv = _normalize_media_blob_form(argv)
        args, forwarded = parser.parse_known_args(argv)
        args.forwarded = forwarded
        if forwarded and args.command not in {"media", "ofw"}:
            parser.error("unrecognized arguments: " + " ".join(forwarded))
        return args.handler(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"gwprov: {exc}", file=sys.stderr)
        return 2
