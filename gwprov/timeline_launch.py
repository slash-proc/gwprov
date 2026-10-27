"""Configure GWemu's native guest-clock input recorder and player."""

from pathlib import Path
import os


def validate_profile_recording(record_profile=None, profile_interval_ms=None, profile_symbols=None):
    """Validate requested recorder settings before contacting the daemon."""
    symbols = list(profile_symbols or ())
    if record_profile is None:
        if profile_interval_ms is not None or symbols:
            raise ValueError("--profile-interval-ms and --profile-symbols require --record-profile")
        return None, None, []
    interval = 100 if profile_interval_ms is None else profile_interval_ms
    if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 60000:
        raise ValueError("--profile-interval-ms must be an integer from 1 to 60000")
    path = Path(record_profile).expanduser().resolve()
    if path.exists() and path.is_dir():
        raise ValueError(f"profile recording output is a directory: {path}")
    resolved_symbols = []
    for raw in symbols:
        manifest = Path(raw).expanduser().resolve()
        if not manifest.is_file():
            raise ValueError(f"profile symbols manifest not found: {manifest}")
        resolved_symbols.append(manifest)
    return path, interval, resolved_symbols


def configure_profile_recording(
    env: dict, *, record_profile=None, profile_interval_ms=None, profile_symbols=None
):
    """Configure bounded guest timeline recording without inheriting daemon settings."""
    for key in tuple(env):
        if key.startswith("GNW_PROFILE_"):
            env.pop(key, None)
    path, interval, symbols = validate_profile_recording(
        record_profile, profile_interval_ms, profile_symbols
    )
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    env["GNW_PROFILE_RECORD"] = str(path)
    env["GNW_PROFILE_INTERVAL_NS"] = str(interval * 1_000_000)
    if symbols:
        env["GNW_PROFILE_SYMBOLS"] = os.pathsep.join(str(item) for item in symbols)
    print(f"Recording GWemu timeline profile to {path} at {interval} ms intervals.", flush=True)


def configure_timeline(env: dict, *, timeline=None, record_timeline=None, headless=False):
    """Validate before launching; never overwrite an existing recorded route."""
    if timeline and record_timeline:
        raise ValueError("choose --timeline or --record-timeline for this launch")
    if record_timeline and headless:
        raise ValueError("--record-timeline requires a visible GWemu window")
    if timeline:
        path = Path(timeline).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"input timeline not found: {path}")
        env["GNW_TIMELINE"] = str(path)
        env.pop("GNW_TIMELINE_RECORD", None)
        print(f"Replaying input timeline: {path}", flush=True)
    if record_timeline:
        path = Path(record_timeline).expanduser().resolve()
        if path.exists():
            raise ValueError(f"recording already exists: {path}; choose a new filename")
        path.parent.mkdir(parents=True, exist_ok=True)
        env["GNW_TIMELINE_RECORD"] = str(path)
        env.pop("GNW_TIMELINE", None)
        print(
            f"Recording GUI inputs to {path} using guest time; "
            "close GWemu cleanly when finished.",
            flush=True,
        )
