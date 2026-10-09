"""Command-line entry point for common provisioning operations."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path


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


def _profile_stock(args) -> int:
    from gwprov.stock import create_stock_profile
    result = create_stock_profile(args.directory, backup_dirs=args.backup_dir,
                                  locked=args.locked, model=args.model,
                                  extflash_mib=args.extflash_mib)
    print(f"Created stock {result['model']} profile: {args.directory}; locked={result['locked']}")
    return 0


def _profile_create(args) -> int:
    from gwprov.provision import create_profile
    report = create_profile(args.directory, content=args.content, name=args.name,
                            littlefs_mib=args.littlefs_mib, extflash_mib=args.extflash_mib,
                            sd_size_mib=args.sd_size_mib, sd_label=args.sd_label,
                            bootloader_repo=args.bootloader_repo,
                            bootloader_version=args.bootloader_version,
                            bootloader_file=args.bootloader_file)
    if report.get('variant') == 'sd':
        print(f"Created {args.directory}: SD image {report['layout']['sdImageBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    else:
        print(f"Created {args.directory}: FrogFS {report['layout']['frogfsBytes']} bytes; "
              f"LittleFS {report['layout']['littlefsBytes']} bytes; "
              f"{report['layout']['extflashBytes'] // (1024 * 1024)} MiB extflash")
    print(f"Boot with: gwprov gwemu run --profile {args.directory}")
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
        from gwprov.launch import launch_profile
        if args.stdio_gdb:
            raise ValueError("profile launch runs freely; use --gdb-port for an optional debug connection")
        return launch_profile(args.profile, headless=args.headless, audio=args.audio,
                              timeline=args.timeline, record_timeline=args.record_timeline,
                              gdb_port=args.gdb_port,
                              qmp_socket=args.qmp_socket)
    from gwprov.common.target import GwemuTarget, Image
    from gwprov.profiles import DeviceProfile

    if args.profile:
        profile = DeviceProfile.load(args.profile, shared_sd_root=args.shared_sd_root)
        image = profile.image(intflash_bank=args.bank)
    else:
        image = Image(
            bank1=args.bank1,
            bank2=args.bank2,
            extflash=args.extflash,
            sdcard=args.sdcard,
            intflash_bank=args.bank,
        )
    target = GwemuTarget(
        image,
        display=not args.headless,
        audio=args.audio,
        icount=args.icount,
        timeline=args.timeline,
        record=args.record_timeline,
        keep_temp=args.keep_temp,
        stdio_gdb=args.stdio_gdb,
    )
    try:
        target.start()
        print("GWemu running; press Ctrl-C to stop.", flush=True)
        while target.proc is not None and target.proc.poll() is None:
            time.sleep(0.5)
        return target.proc.returncode if target.proc else 0
    except KeyboardInterrupt:
        return 130
    finally:
        target.stop()


def _gwemu_start(args) -> int:
    from gwprov.gwemu_manager import start_instance
    return start_instance(args.profile, audio=args.audio, gdb_port=args.gdb_port,
                          qmp_socket=args.qmp_socket, headless=args.headless,
                          timeline=args.timeline, record_timeline=args.record_timeline)


def _gwemu_stop(args) -> int:
    from gwprov.gwemu_manager import stop_instance
    return stop_instance(pid=args.pid, profile=args.profile, timeout=args.timeout)

def _gwemu_pause(args) -> int:
    from gwprov.gwemu_manager import set_instance_running
    return set_instance_running(args.profile, running=False)


def _gwemu_resume(args) -> int:
    from gwprov.gwemu_manager import set_instance_running
    return set_instance_running(args.profile, running=True)


def _gwemu_ps(args) -> int:
    from gwprov.gwemu_manager import show_instances
    return show_instances(output=args.output)


def _gwemu_screenshot(args) -> int:
    from gwprov.gwemu_manager import screenshot_instance
    return screenshot_instance(pid=args.pid, profile=args.profile, output=args.output)


def _gwemu_profile(args) -> int:
    from gwprov.profiling import profile_instance
    return profile_instance(args.profile, duration=args.duration, interval=args.interval,
                            symbols=args.symbols, progress_symbols=args.progress_symbol,
                            rebase_symbols=args.rebase, output=args.output,
                            output_format=args.format, top=args.top, debug_config=args.debug_config,
                            stall_threshold=args.stall_threshold)


def _gwemu_diagnose(args) -> int:
    from gwprov.gwemu_manager import diagnose_instance
    return diagnose_instance(args.profile, symbols=args.symbols,
                             output=args.output, max_frames=args.max_frames,
                             inspect_u32=args.u32, inspect_deref=args.deref,
                             inspect_bytes=args.bytes,
                             inspect_values=args.value, inspect_rings=args.ring, debug_config=args.debug_config)

def _gwemu_watch(args) -> int:
    from gwprov.gwemu_manager import watch_instance
    return watch_instance(args.profile, symbols=args.symbols,
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
    return debug_profile(args.profile, gdb_port=args.gdb_port,
                         headless=args.headless,
                         qmp_socket=args.qmp_socket, qmp_enabled=not args.no_qmp,
                         symbols=args.symbols, gdb=args.gdb,
                         audio=args.audio, break_on_fault=not args.no_break_on_fault,
                         unpause_homebrew=args.unpause_homebrew,
                         app_symbols=args.app_symbols,
                         detach_after_app_entry=args.detach_after_app_entry,
                         keep_running=args.keep_running, timeline=args.timeline,
                         record_timeline=args.record_timeline)


def _ps(args) -> int:
    from gwprov.devices import show_devices
    return show_devices(output=args.output, profile=args.profile)


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


def _deploy_apply(args) -> int:
    from gwprov.deploy import apply_deployment
    result = apply_deployment(args.profile, probe_id=args.probe_id, programmer=args.programmer,
                              remote_url=args.remote_url, remote_origin=args.remote_origin,
                              regions=args.region)
    print(json.dumps(result, indent=2))
    return 0


def _profile_hardware(args) -> int:
    from gwprov.hw_profile import profile_hardware
    return profile_hardware(probe_id=args.probe_id, programmer=args.programmer,
                            remote_url=args.remote_url,
                            remote_origin=args.remote_origin, profile=args.profile,
                            symbols=args.symbols, duration=args.duration,
                            interval=args.interval, output=args.output,
                            output_format=args.format, top=args.top)


def _report_render(args) -> int:
    from gwprov.reports import render_report
    output = render_report(args.input, args.output, format=args.format)
    print(f"Rendered {args.format.upper()} report: {output}")
    return 0


def _debug_python(args) -> int:
    from gwprov.debug_shell import python_shell

    symbol_paths = list(args.symbols)
    if args.profile:
        from gwprov.profiles import DeviceProfile
        firmware_symbols = DeviceProfile.load(args.profile).root / "debug" / "retro-go-debug.elf"
        if not firmware_symbols.is_file():
            raise ValueError(f"profile has no bundled firmware symbols: {firmware_symbols}")
        symbol_paths.insert(0, str(firmware_symbols))
        app_symbols = sorted((firmware_symbols.parent / "apps").rglob("*.elf"))
        symbol_paths[1:1] = [str(path) for path in app_symbols]
    return python_shell(target=args.target, host=args.host, port=args.port,
                        openocd_port=args.openocd_port, probe_ids=args.probe_id,
                        programmers=args.programmer, remote_urls=args.remote_url,
                        remote_origins=args.remote_origin,
                        symbols=symbol_paths,
                        qmp_socket=args.qmp_socket, debug_config=args.debug_config)


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
    from gwprov.remote_input import NAMES, session

    names = [part.strip().upper() for part in args.buttons.split("+")]
    unknown = [name for name in names if name not in NAMES]
    if unknown:
        raise ValueError(f"unknown button(s): {', '.join(unknown)}")
    keys = [NAMES[name] for name in names]
    with session() as dev:
        dev.tap(keys, repeat=args.repeat, tap_ms=args.tap_ms, gap_ms=args.gap_ms)
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
    catalog=$(gwprov project list --output json 2>/dev/null) || return
    _GWPROV_PROJECT_NAMES=$(python3 -c 'import json,sys,urllib.parse; projects=json.load(sys.stdin)["projects"]; [(print(p["project"]), print((urllib.parse.urlparse(p["versionsUrl"]).hostname or "").split(".",1)[0]+"/"+p["project"])) for p in projects]' <<< "$catalog")
  fi
  printf '%s\n' "$_GWPROV_PROJECT_NAMES"
}
_gwprov_complete_dirs() {
  local token="$1" prefix="" pathpart search match completed
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
    COMPREPLY+=("${prefix}${completed%/}/")
  done < <(compgen -d -- "$search")
}
_gwprov_complete() {
  local cur prev context candidates extra_candidates candidate
  cur="${COMP_WORDS[COMP_CWORD]}"
  prev=""
  if (( COMP_CWORD > 0 )); then prev="${COMP_WORDS[COMP_CWORD-1]-}"; fi
  context="${COMP_WORDS[1]-}:${COMP_WORDS[2]-}"
  extra_candidates=""
  case "$cur" in
    --input-dir=*|--firmware-dir=*|--bios-dir=*|--game-dir=*|--content=*|--profile=*|--output=*|--bootloader-file=*|--backup-dir=*)
      _gwprov_complete_dirs "$cur"
      return
      ;;
    --variant=*)
      COMPREPLY=()
      while IFS= read -r candidate; do COMPREPLY+=("--variant=$candidate"); done < <(compgen -W "flash sd" -- "${cur#*=}")
      return
      ;;
  esac
  if [[ "$prev" == "--variant" ]]; then
    COMPREPLY=( $(compgen -W "flash sd" -- "$cur") )
    return
  fi
  if [[ "$prev" == "--input-dir" || "$prev" == "--firmware-dir" || "$prev" == "--bios-dir" || "$prev" == "--game-dir" || "$prev" == "--content" || "$prev" == "--profile" || "$prev" == "--output" || "$prev" == "--bootloader-file" || "$prev" == "--backup-dir" ]]; then
    _gwprov_complete_dirs "$cur"
    return
  fi
  if (( COMP_CWORD == 1 )); then
    candidates=$(gwprov tree 2>/dev/null | awk 'substr($0,1,2)=="  " && substr($0,3,1)!=" " {sub(/^  /, ""); sub(/ — .*/, ""); print}')
  elif (( COMP_CWORD == 2 )); then
    case "${COMP_WORDS[1]}" in
      project) candidates="list versions info install stage-local" ;;
      retro-go) candidates="install build config" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sd) candidates="create compose" ;;
      input) candidates="tap" ;;
      profile) candidates="create stock show" ;;
      completion) candidates="bash zsh" ;;
      *) candidates="" ;;
    esac
  elif (( COMP_CWORD == 3 )) && [[ "${COMP_WORDS[1]-}" == project ]]; then
    case "${COMP_WORDS[2]-}" in
      list|versions|info|install) candidates=$(_gwprov_project_names) ;;
      stage-local)
        COMPREPLY=()
        while IFS= read -r candidate; do COMPREPLY+=("$candidate"); done < <(compgen -f -- "$cur")
        return
        ;;
      *) candidates="" ;;
    esac
  else
    extra_candidates="--help -h"
    case "$context" in
      project:list) candidates="--firmware-repo --output" ;;
      project:versions) candidates="--output" ;;
      project:info) candidates="--version --output" ;;
      project:install) candidates="--version --target --variant --output --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run" ;;
      project:stage-local) candidates="--output" ;;
      gwemu:start) candidates="--profile --gdb-port --qmp-socket --audio --headless --timeline --record-timeline" ;;
      gwemu:stop) candidates="--profile --pid --timeout" ;;
      gwemu:pause|gwemu:resume) candidates="--profile" ;;
      gwemu:ps) candidates="--output" ;;
      gwemu:screenshot) candidates="--profile --pid --output" ;;
      gwemu:profile) candidates="--profile --symbols --duration --interval --progress-symbol --stall-threshold --rebase --debug-config --output --format --top" ;;
      gwemu:diagnose) candidates="--profile --symbols --output --max-frames --u32 --deref --bytes --debug-config" ;;
      gwemu:watch) candidates="--profile --symbols --progress-symbol --guest-pc-symbol --rebase --u32 --deref --bytes --interval --stall-after --duration --output --debug-config" ;;
      gwemu:run) candidates="--profile --gdb-port --qmp-socket --shared-sd-root --bank1 --bank2 --extflash --sdcard --bank --timeline --record-timeline --icount --headless --audio --keep-temp --stdio-gdb" ;;
      gwemu:debug) candidates="--profile --gdb-port --headless --qmp-socket --no-qmp --symbols --gdb --audio --no-break-on-fault --unpause-homebrew --app-symbols --detach-after-app-entry --keep-running --timeline --record-timeline" ;;
      debug:python) candidates="--target --host --port --openocd-port --probe-id --programmer --remote-url --remote-origin --qmp-socket --profile --symbols --debug-config" ;;
      media:inventory) candidates="--profile --image --filesystem --offset --size --block-size --shared-sd-root --output" ;;
      media:compare) candidates="--mode" ;;
      profile:stock) candidates="--backup-dir --locked --model --extflash-mib" ;;
      profile:create) candidates="--content --name --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file" ;;
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
  catalog=$(gwprov project list --output json 2>/dev/null) || return
  print -r -- "${(f)$(python3 -c 'import json,sys,urllib.parse; projects=json.load(sys.stdin)["projects"]; [(print(p["project"]), print((urllib.parse.urlparse(p["versionsUrl"]).hostname or "").split(".",1)[0]+"/"+p["project"])) for p in projects]' <<< "$catalog")}"
}
_gwprov() {
  local cur prev context candidates prefix pathpart
  local -a words_to_add
  cur=${words[CURRENT]}
  prev=${words[CURRENT-1]}
  context="${words[2]}:${words[3]}"

  case "$cur" in
    --input-dir=*|--firmware-dir=*|--bios-dir=*|--game-dir=*|--content=*|--profile=*|--output=*|--bootloader-file=*|--backup-dir=*)
      prefix=${cur%%=*}=; pathpart=${cur#*=}
      IPREFIX=$prefix PREFIX=$pathpart _files -/
      return
      ;;
    --variant=*) IPREFIX=--variant= PREFIX=${cur#*=}; compadd -- flash sd; return ;;
  esac
  case "$prev" in
    --input-dir|--firmware-dir|--bios-dir|--game-dir|--content|--profile|--output|--bootloader-file|--backup-dir)
      _files -/; return ;;
    --variant) compadd -- flash sd; return ;;
  esac

  if (( CURRENT == 2 )); then
    candidates="$(gwprov tree 2>/dev/null | awk 'substr($0,1,2)=="  " && substr($0,3,1)!=" " {sub(/^  /, ""); sub(/ — .*/, ""); print}')"
  elif (( CURRENT == 3 )); then
    case ${words[2]} in
      project) candidates="list versions info install stage-local" ;;
      retro-go) candidates="install build config" ;;
      gwemu) candidates="start stop pause resume ps screenshot profile diagnose watch run debug" ;;
      ofw) candidates="patch" ;;
      media) candidates="frogfs littlefs inventory compare" ;;
      sd) candidates="create compose" ;;
      input) candidates="tap" ;;
      profile) candidates="create stock show" ;;
      debug) candidates="python" ;;
      completion) candidates="bash zsh" ;;
      *) candidates="" ;;
    esac
  elif (( CURRENT == 4 )) && [[ ${words[2]} == project ]]; then
    case ${words[3]} in
      list|versions|info|install) candidates="$( _gwprov_project_names )" ;;
      stage-local) _files; return ;;
      *) candidates="" ;;
    esac
  else
    case "$context" in
      project:list) candidates="--firmware-repo --output" ;;
      project:versions) candidates="--output" ;;
      project:info) candidates="--version --output" ;;
      project:install) candidates="--version --target --variant --output --input --input-dir --firmware --firmware-dir --bios --bios-dir --game --game-dir --dry-run" ;;
      project:stage-local) candidates="--output" ;;
      gwemu:start) candidates="--profile --gdb-port --qmp-socket --audio --headless --timeline --record-timeline" ;;
      gwemu:stop) candidates="--profile --pid --timeout" ;;
      gwemu:pause|gwemu:resume) candidates="--profile" ;;
      gwemu:ps) candidates="--output" ;;
      gwemu:screenshot) candidates="--profile --pid --output" ;;
      gwemu:profile) candidates="--profile --symbols --duration --interval --progress-symbol --stall-threshold --rebase --debug-config --output --format --top" ;;
      gwemu:diagnose) candidates="--profile --symbols --output --max-frames --u32 --deref --bytes --debug-config" ;;
      gwemu:watch) candidates="--profile --symbols --progress-symbol --guest-pc-symbol --rebase --u32 --deref --bytes --interval --stall-after --duration --output --debug-config" ;;
      gwemu:run) candidates="--profile --gdb-port --qmp-socket --shared-sd-root --bank1 --bank2 --extflash --sdcard --bank --timeline --record-timeline --icount --headless --audio --keep-temp --stdio-gdb" ;;
      gwemu:debug) candidates="--profile --gdb-port --headless --qmp-socket --no-qmp --symbols --gdb --audio --no-break-on-fault --unpause-homebrew --app-symbols --detach-after-app-entry --keep-running --timeline --record-timeline" ;;
      debug:python) candidates="--target --host --port --openocd-port --probe-id --programmer --remote-url --remote-origin --qmp-socket --profile --symbols --debug-config" ;;
      media:inventory) candidates="--profile --image --filesystem --offset --size --block-size --shared-sd-root --output" ;;
      media:compare) candidates="--mode" ;;
      profile:stock) candidates="--backup-dir --locked --model --extflash-mib" ;;
      profile:create) candidates="--content --name --littlefs-mib --extflash-mib --sd-size-mib --sd-label --bootloader-repo --bootloader-version --bootloader-file" ;;
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
    print("gwprov")

    def walk(current: argparse.ArgumentParser, depth: int) -> None:
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                help_text = descriptions.get(name) or child.description or ""
                suffix = f" — {help_text}" if help_text else ""
                print(f"{'  ' * depth}{name}{suffix}")
                walk(child, depth + 1)

    walk(parser, 1)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwprov", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    ps = commands.add_parser("ps", help="list GWemu instances and local hardware probes")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.add_argument("--profile", help="load firmware/app symbols for physical Application state")
    ps.set_defaults(handler=_ps)
    deploy = commands.add_parser("deploy", help="plan and provision a physical Game & Watch")
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
    deploy_apply.add_argument("--region", action="append", choices=("bank1", "bank2", "frogfs", "littlefs", "extflash", "sd"),
                              help="region to deploy; repeatable, defaults to all profile regions")
    deploy_apply.set_defaults(handler=_deploy_apply)
    tree = commands.add_parser("tree", help="list all commands and their short descriptions")
    tree.set_defaults(handler=_command_tree)
    completion = commands.add_parser("completion", help="print shell completion setup")
    completion.add_argument("shell", choices=("bash", "zsh"))
    completion.set_defaults(handler=lambda args: _completion_bash(args) if args.shell == "bash" else _completion_zsh(args))

    debug_tools = commands.add_parser("debug", help="interactive target debugging")
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
    python_debug.add_argument("--qmp-socket", help="GWemu QMP socket for screendump support")
    python_debug.add_argument("--debug-config", help="local ELF/map, relocation and counter descriptor")
    python_debug.add_argument("--profile", help="load the profile's bundled official Retro-Go ELF symbols")
    python_debug.add_argument("--symbols", action="append", default=[], metavar="ELF",
                              help="load an additional firmware or app ELF (repeatable)")
    python_debug.set_defaults(handler=_debug_python)

    project = commands.add_parser("project", help="resolve and stage GWRG-distributed projects")
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

    retro_go = commands.add_parser("retro-go", help="vendor-supported Retro-Go operations")
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

    gwemu = commands.add_parser("gwemu", help="run a provisioned GWemu profile or image")
    gwemu_commands = gwemu.add_subparsers(dest="gwemu_command", required=True)
    start = gwemu_commands.add_parser("start", help="start one managed GWemu profile")
    start.add_argument("--profile", required=True)
    start.add_argument("--gdb-port", type=int, help="GDB port; default: choose an available port")
    start.add_argument("--qmp-socket", help="QMP socket; default: profile runtime directory")
    start.add_argument("--audio", action="store_true")
    start.add_argument("--headless", action="store_true",
                       help="run without the window (visible by default)")
    start_inputs = start.add_mutually_exclusive_group()
    start_inputs.add_argument("--timeline", help="replay inputs on GWemu guest time")
    start_inputs.add_argument("--record-timeline", metavar="FILE.tl",
                              help="record GUI inputs on guest time; requires a new file")
    start.set_defaults(handler=_gwemu_start)

    stop = gwemu_commands.add_parser("stop", help="gracefully stop an instance through QMP")
    stop_target = stop.add_mutually_exclusive_group(required=True)
    stop_target.add_argument("--profile", help="profile directory of the instance")
    stop_target.add_argument("--pid", type=int, help="GWemu process id")
    stop.add_argument("--timeout", type=float, default=10.0)
    stop.set_defaults(handler=_gwemu_stop)

    pause = gwemu_commands.add_parser("pause", help="pause a GWemu instance through QMP")
    pause.add_argument("--profile", required=True)
    pause.set_defaults(handler=_gwemu_pause)

    resume = gwemu_commands.add_parser("resume", help="resume a paused GWemu instance through QMP")
    resume.add_argument("--profile", required=True)
    resume.set_defaults(handler=_gwemu_resume)

    ps = gwemu_commands.add_parser("ps", help="list running GWemu instances and endpoints")
    ps.add_argument("--output", choices=("text", "json"), default="text")
    ps.set_defaults(handler=_gwemu_ps)

    screenshot = gwemu_commands.add_parser(
        "screenshot", help="capture a GWemu PNG and report whether the frame is black")
    screenshot_target = screenshot.add_mutually_exclusive_group(required=True)
    screenshot_target.add_argument("--profile", help="profile directory of the instance")
    screenshot_target.add_argument("--pid", type=int, help="GWemu process id")
    screenshot.add_argument("--output", help="PNG path; defaults under the profile runtime directory")
    screenshot.set_defaults(handler=_gwemu_screenshot)

    profiler = gwemu_commands.add_parser(
        "profile", help="sample native hot functions through QMP while the app runs")
    profiler.add_argument("--profile", required=True)
    profiler.add_argument("--symbols", action="append", default=[], metavar="ELF",
                          help="additional symbols; bundled firmware/app ELFs load automatically")
    profiler.add_argument("--debug-config", help="local ELF/map, relocation and counter descriptor")
    profiler.add_argument("--duration", type=float, default=15.0)
    profiler.add_argument("--interval", type=float, default=0.02,
                          help="sample interval in wall seconds, with jitter (default: 0.02)")
    profiler.add_argument("--progress-symbol", action="append", metavar="SYMBOL",
                          help="32-bit progress counter; default: discover common counter names")
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
    diagnose.add_argument("--profile", required=True,
                          help="profile directory of the running GWemu instance")
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
    watch.add_argument("--profile", required=True)
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

    run = gwemu_commands.add_parser("run", help="launch GWemu using the shared target layer")
    run.add_argument("--profile")
    run.add_argument("--gdb-port", type=int, help="optional debug server; guest starts running")
    run.add_argument("--qmp-socket", help="optional QMP UNIX socket for emulator controls")
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
    run.add_argument("--icount", type=int)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--audio", action="store_true")
    run.add_argument("--keep-temp", action="store_true")
    run.add_argument("--stdio-gdb", action="store_true")
    run.set_defaults(handler=_run_gwemu)

    debug = gwemu_commands.add_parser("debug", help="run GWemu under GDB with bundled firmware symbols")
    debug.add_argument("--profile", required=True)
    debug.add_argument("--gdb-port", type=int, default=1234)
    debug.add_argument("--headless", action="store_true",
                        help="run without a display window")
    qmp_options = debug.add_mutually_exclusive_group()
    qmp_options.add_argument("--qmp-socket", help="custom QMP UNIX socket path (enabled by default)")
    qmp_options.add_argument("--no-qmp", action="store_true",
                             help="disable the default QMP endpoint")
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

    ofw = commands.add_parser("ofw", help="offline stock firmware image preparation")
    ofw_commands = ofw.add_subparsers(dest="ofw_command", required=True)
    patch = ofw_commands.add_parser("patch", help="build patched stock banks using gnwmanager's offline patch pipeline")
    patch.add_argument("game", choices=("mario", "zelda"))
    patch.add_argument("--source-tree", required=True, help="qemu-gnw checkout")
    patch.add_argument("--backup-dir", required=True, help="directory of stock internal and external backups")
    patch.add_argument("--output-dir", required=True)
    patch.set_defaults(handler=_ofw_patch)

    media = commands.add_parser("media", help="build Retro-Go filesystem images")
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

    sd = commands.add_parser("sd", help="create and compose Retro-Go SD card images")
    sd_commands = sd.add_subparsers(dest="sd_command", required=True)
    sd_create = sd_commands.add_parser("create", help="create a partitioned FAT32 SD image")
    sd_create.add_argument("image")
    sd_create.add_argument("--size-mb", type=int, required=True)
    sd_create.add_argument("--label", default="RETROGO")
    sd_create.set_defaults(handler=_sd_create)
    sd_compose = sd_commands.add_parser("compose", help="copy selected Retro-Go content onto an image")
    sd_compose.add_argument("image")
    sd_compose.add_argument("--content-dir")
    sd_compose.add_argument("--core")
    sd_compose.add_argument("--core-name", default="dos")
    sd_compose.add_argument("--rom", action="append", default=[])
    sd_compose.add_argument("--rom-dir", default="dos")
    sd_compose.add_argument("--config")
    sd_compose.set_defaults(handler=_sd_compose)

    input_group = commands.add_parser("input", help="inject buttons through REMOTE_INPUT")
    input_commands = input_group.add_subparsers(dest="input_command", required=True)
    tap = input_commands.add_parser("tap", help="tap a button or chord over the probe")
    tap.add_argument("buttons", help="button name or chord, such as LEFT+GAME")
    tap.add_argument("--repeat", type=int, default=1)
    tap.add_argument("--tap-ms", type=int, default=80)
    tap.add_argument("--gap-ms", type=int, default=120)
    tap.set_defaults(handler=_input_tap)

    reports = commands.add_parser("report", help="render structured reports for human review")
    report_commands = reports.add_subparsers(dest="report_command", required=True)
    render = report_commands.add_parser("render", help="render JSON as portable HTML or PDF")
    render.add_argument("--input", required=True)
    render.add_argument("--output", required=True)
    render.add_argument("--format", choices=("html", "pdf"), required=True)
    render.set_defaults(handler=_report_render)

    profile = commands.add_parser("profile", help="create and inspect provisioned GWemu instances")
    profile_commands = profile.add_subparsers(dest="profile_command", required=True)
    create = profile_commands.add_parser("create", help="pack flash or SD content into a bootable GWemu instance")
    create.add_argument("directory")
    create.add_argument("--content", required=True, help="root populated by retro-go install and project install")
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
    hardware_profile = profile_commands.add_parser(
        "hardware", help="profile routine cycles from a non-halting GWProv trace ring")
    profile_target = hardware_profile.add_mutually_exclusive_group()
    profile_target.add_argument("--probe-id", help="select one local PyOCD probe by unique ID")
    profile_target.add_argument("--programmer", choices=("stlink", "jlink", "cmsis-dap", "rpi-gpio"),
                                help="select one local OpenOCD adapter explicitly")
    profile_target.add_argument("--remote-url", help="use one gnwmanager serve URL")
    hardware_profile.add_argument("--remote-origin", help="Origin required by the selected remote server")
    hardware_profile.add_argument("--profile", help="load firmware and app symbols from a device profile")
    hardware_profile.add_argument("--symbols", action="append", default=[], metavar="ELF",
                                  help="additional firmware/app ELF symbols")
    hardware_profile.add_argument("--duration", type=float, default=10.0)
    hardware_profile.add_argument("--interval", type=float, default=0.05)
    hardware_profile.add_argument("--output", help="report path; default under dev-local/reports")
    hardware_profile.add_argument("--format", choices=("text", "json", "html", "pdf"), default="text")
    hardware_profile.add_argument("--top", type=int, default=10)
    hardware_profile.set_defaults(handler=_profile_hardware)
    stock = profile_commands.add_parser("stock", help="create pristine stock media from hash-valid backups")
    stock.add_argument("directory")
    stock.add_argument("--backup-dir", action="append", required=True)
    stock.add_argument("--locked", action="store_true", help="initialize device RDP state as locked")
    stock.add_argument("--model", choices=("auto", "mario", "zelda"), default="auto")
    stock.add_argument("--extflash-mib", type=int, choices=(64, 128, 256), default=64)
    stock.set_defaults(handler=_profile_stock)
    show = profile_commands.add_parser("show")
    show.add_argument("directory")
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
