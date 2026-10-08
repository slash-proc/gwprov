"""Configure GWemu's native guest-clock input recorder and player."""
from pathlib import Path


def configure_timeline(env: dict, *, timeline=None, record_timeline=None,
                       headless=False):
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
        print(f"Recording GUI inputs to {path} using guest time; "
              "close GWemu cleanly when finished.", flush=True)
