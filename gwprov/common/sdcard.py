"""
SD card access and content composition.

Three destinations, one interface -- which is the whole reason this is Python
and not mtools calls in a Makefile:

  * QemuSDCardManager           mcopy into an image file (gwemu)
  * HardwareProbeSDCardManager  sd_write_file over the debug probe (sdpush)
  * HardwarePhysicalSDCardManager  copy to a mounted block device (bulk --
                                repackaging DOS ROMs)

`compose()` describes WHAT belongs on the card; the manager decides WHERE it
goes. That split is what lets the same content reach an emulator image, a
running device, and a card in a reader without three copies of the recipe.
"""

import os
import shutil
import subprocess
import sys

# The firmware expects an MBR with a FAT32 primary partition starting at 1 MiB.
# mtools addresses that as `img@@1M`. A bare filesystem image is simply not
# found -- the launcher boots to an empty carousel with no error at all.
MTOOLS_PARTITION_OFFSET = "@@1M"


class SDCardManager:
    def push_file(self, local_path, remote_path):
        raise NotImplementedError

    def mkdir(self, remote_path):
        """Create a directory. Must tolerate it already existing."""
        raise NotImplementedError

    def remove(self, remote_path):
        """Delete a file. Must tolerate it being absent.

        Needed because composing onto an EXISTING card is not the same as
        composing onto a fresh one: whatever the last run left behind is still
        there. /CONFIG is the one that bites -- it preselects a ROM.
        """
        raise NotImplementedError


class QemuSDCardManager(SDCardManager):
    """An SD image file, reached with mtools."""

    def __init__(self, img_path, partition_offset=MTOOLS_PARTITION_OFFSET):
        self.img_path = img_path
        # Callers holding a bare (unpartitioned) image pass "".
        self.spec = f"{img_path}{partition_offset}"

    def push_file(self, local_path, remote_path):
        print(f"[SD image] {local_path} -> ::{remote_path}")
        subprocess.run(["mcopy", "-i", self.spec, "-o", local_path,
                        f"::{remote_path}"], check=True)

    def mkdir(self, remote_path):
        subprocess.run(["mmd", "-i", self.spec, f"::{remote_path}"],
                       check=False, capture_output=True)

    def remove(self, remote_path):
        subprocess.run(["mdel", "-i", self.spec, f"::{remote_path}"],
                       check=False, capture_output=True)

    def listing(self, remote_path="/"):
        return subprocess.run(["mdir", "-i", self.spec, f"::{remote_path}"],
                              capture_output=True, text=True).stdout


class HardwarePhysicalSDCardManager(SDCardManager):
    """A card mounted on this host. The bulk path."""

    def __init__(self, mount_path):
        self.mount_path = mount_path

    def push_file(self, local_path, remote_path):
        dest = os.path.join(self.mount_path, remote_path.lstrip("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        print(f"[SD mount] {local_path} -> {dest}")
        shutil.copy(local_path, dest)

    def mkdir(self, remote_path):
        os.makedirs(os.path.join(self.mount_path, remote_path.lstrip("/")),
                    exist_ok=True)

    def remove(self, remote_path):
        try:
            os.remove(os.path.join(self.mount_path, remote_path.lstrip("/")))
        except FileNotFoundError:
            pass


class HardwareProbeSDCardManager(SDCardManager):
    """The device's own card, written over the debug probe. The sdpush path."""

    def __init__(self, gnw_instance):
        self.gnw = gnw_instance

    def push_file(self, local_path, remote_path):
        # gnwmanager rejects a relative path outright ("path shall start with
        # '/'"), while the image and mount managers want one relative to the
        # card root. compose() speaks the relative form, so anchor it here
        # rather than making every caller know which manager it holds.
        remote = remote_path if remote_path.startswith("/") else "/" + remote_path
        print(f"[SD probe] {local_path} -> {remote}")
        with open(local_path, "rb") as f:
            self.gnw.sd_write_file(remote, f.read())

    def mkdir(self, remote_path):
        # gnwmanager creates parents on write; nothing to do.
        pass

    def remove(self, remote_path):
        remote = remote_path if remote_path.startswith("/") else "/" + remote_path
        # No delete in the gnwmanager API we use; truncating to zero bytes is
        # enough for /CONFIG, whose parser rejects a short file.
        self.gnw.sd_write_file(remote, b"")


def create_image(path, size_mb=128, label="RETROGO", script=None):
    """Create a partitioned, formatted SD image.

    Uses the firmware's own make_sdcard_image.py (vendored) for the MBR, then
    mformat inside the partition -- the same two steps its Makefile.common does.
    """
    script = script or os.path.join(os.path.dirname(__file__), "..",
                                    "make_sdcard_image.py")
    subprocess.run([sys.executable, script, path, "--size-mb", str(size_mb)],
                   check=True)
    subprocess.run(["mformat", "-i", f"{path}{MTOOLS_PARTITION_OFFSET}",
                    "-F", "-v", label, "::"], check=True)
    return path


def push_tree(manager, local_dir, *, exclude_names=()):
    """Copy a directory tree onto the card, preserving layout."""
    local_dir = os.fspath(local_dir).rstrip("/")
    excluded = set(exclude_names)
    for root, dirs, files in os.walk(local_dir):
        dirs[:] = sorted(name for name in dirs if name not in excluded)
        rel = os.path.relpath(root, local_dir)
        remote_dir = "" if rel == "." else rel.replace(os.sep, "/")
        if remote_dir:
            manager.mkdir(remote_dir)
        for name in sorted(files):
            if name in excluded:
                continue
            remote = f"{remote_dir}/{name}" if remote_dir else name
            manager.push_file(os.path.join(root, name), remote)


def compose(manager, core_bin=None, core_name="dos", rom=None, rom_dir="dos",
            config=None, content_dir=None, sidecars=True):
    """Lay out everything the firmware needs, onto any destination.

    content_dir  the firmware's sd_content tree (fonts, lang, bios). Without
                 fonts the launcher renders empty frames and nothing else.
    core_bin     packed core -> /cores/<core_name>.bin (creates the system tab)
    rom          guest .dsk -> /roms/<rom_dir>/, with .cfg/.dosmeta sidecars
    config       /CONFIG -- preselects the ROM and disables idle standby
    """
    if content_dir:
        push_tree(manager, content_dir)

    # cores/ and roms/ are build products in sd_content; a clean firmware build
    # removes them, so create rather than assume.
    for d in ("cores", "roms", f"roms/{rom_dir}"):
        manager.mkdir(d)

    if core_bin:
        manager.push_file(core_bin, f"cores/{core_name}.bin")

    # `rom` may be one path or many: a card for interactive use wants the whole
    # library, an automated capture wants exactly one title.
    roms = [] if not rom else ([rom] if isinstance(rom, str) else list(rom))
    for one in roms:
        base = os.path.splitext(one)[0]
        manager.push_file(one, f"roms/{rom_dir}/{os.path.basename(one)}")
        if sidecars:
            for ext in (".cfg", ".dosmeta", ".jpg"):
                if os.path.exists(base + ext):
                    manager.push_file(base + ext,
                                      f"roms/{rom_dir}/"
                                      f"{os.path.basename(base)}{ext}")

    if config:
        manager.push_file(config, "CONFIG")
    else:
        # Populating an EXISTING card (--keep, or a card from a gwemu profile)
        # leaves any CONFIG already on it in place, so "no config" silently
        # keeps preselecting whatever the last run chose. Not writing one is
        # not the same as not having one.
        manager.remove("CONFIG")
