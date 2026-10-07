# Source and license notes

The package is distributed under GPL-2.0-only because it includes code from the Game & Watch Retro-Go-SD project, including the `REMOTE_INPUT` transport and filesystem media packers. See `LICENSE`.

The Python modules `gwprov.common.target`, `sdcard`, `timeline`, `retrogo_config`, `retrogo_build`, and `make_sdcard_image.py` were carried over from Doug's own `../tinyemu` project. They are not third-party TinyEMU code; no TinyEMU license applies. The local `target.py` also had a GDB-over-stdio addition when copied. The source checkout was not modified.

`gwprov.remote_input` and the filesystem media packers under `gwprov/vendor/retrogo_sd/scripts` come from the Game & Watch Retro-Go-SD project. The remote-input protocol is coupled to that firmware's `REMOTE_INPUT` implementation and shadow-cell definition in `Core/Inc/gw_buttons.h`.

GWemu profile parsing follows `ui/gwemu-profiles.cc` and `ui/gwemu-profiles.hh` in qemu-gnw. The Python layer does not copy the QEMU implementation. Firmware dumps and generated media remain external data and are not included in Git.

The standalone FrogFS builder under `gwprov/vendor/frogfs` is carried over from Retro-Go-SD’s FrogFS submodule. It remains covered by its MPL-2.0 license; the full license is retained in that directory.
