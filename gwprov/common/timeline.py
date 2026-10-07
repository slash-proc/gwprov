"""Input timelines: one `.tl` file, every tier.

A `.tl` is gwemu's own recorded-input format (see TOOLING.md, "Recording an
input timeline"):

    # comment
    <seconds> down <button>
    <seconds> release <button>
    <seconds> quit

Under gwemu, playback is the emulator's built-in `GNW_TIMELINE` env var and
nothing here is used to drive it -- that path already works and is replayed
against EMULATED time, so it is exact. This module exists so the SAME file
drives REAL HARDWARE, which is the only way `--timeline` can mean the same
thing on every tier and a test can stay tier-agnostic.

How hardware injection works
----------------------------
A `REMOTE_INPUT` firmware build ORs a 32-bit shadow word at 0x30001FF4 into the
live button state on every input poll, so a debug-probe write is
indistinguishable from a physical press. That mechanism (and the bit order
below) is vendored from the firmware's own scripts/remote_input.py, because
references/ is gitignored and nothing may depend on it at runtime.

The bit order below is lifted VERBATIM from that proven script (and matches the
remote decode in Core/Src/gw_buttons.c): UP DOWN LEFT RIGHT A B START SELECT
PAUSE GAME TIME PWR. It is its own order -- not `odroid_gamepad_key_t`, not the
`B_*` masks -- and it is not re-derived here.

What we cannot promise
----------------------
gwemu replays against emulated time; on hardware every state change is an SWD
write from the host, with variable latency, while the device keeps running in
between. So:

  * events are delivered LATE, never early, by roughly one write latency;
  * a press shorter than the host's write cadence may be delivered so briefly
    that the firmware's input poll never sees it. Nothing here compensates for
    that -- gnwmanager's OpenOCD link is robust enough for real timelines, and
    inventing an adaptive schedule would only make playback differ from the
    file in ways nobody asked for. `SHORT_PRESS_MS` is the threshold below
    which a press is FLAGGED, so a timeline that presses nothing says so;
  * `report()` states the ACHIEVED schedule (lateness per event, and the hold
    actually delivered for every press) rather than assuming the requested one.

`quit` on hardware
------------------
There is no process to exit. `quit` means STOP INJECTING: all buttons are
released, the shadow word is zeroed and the player finishes. The device keeps
running, and the harness's own `timeline_quit_time()`-derived wait still ends
the run at the same moment it would under gwemu. It is an end-of-input marker,
not a power switch.
"""

from __future__ import annotations

import atexit
import os
import signal
import threading
import time
from dataclasses import dataclass, field

# --- Shadow cell and bit order: keep in sync with the firmware's
#     Core/Inc/gw_buttons.h and scripts/remote_input.py ---
SHADOW_ADDR = 0x30001FF4

BUTTON_BITS = {
    "up": 0, "down": 1, "left": 2, "right": 3,
    "a": 4, "b": 5, "start": 6, "select": 7,
    "pause": 8, "game": 9, "time": 10, "power": 11,
}
BIT_NAME = {v: k for k, v in BUTTON_BITS.items()}

# gwemu's own name for the power button is `pwr`; remote_input.py calls the bit
# PWR. Accept both spellings rather than making a .tl file tier-specific.
BUTTON_ALIASES = {"pwr": "power"}

# The firmware polls buttons once per frame (~16 ms). A press held for less than
# this is not guaranteed to be seen on hardware -- it is not corrected, only
# reported, so a test never quietly presses nothing.
SHORT_PRESS_MS = 20


def mask_str(mask: int) -> str:
    parts = [BIT_NAME[b] for b in sorted(BIT_NAME) if mask & (1 << b)]
    return "+".join(parts) if parts else "none"


@dataclass(frozen=True)
class Event:
    t: float                # seconds from the start of the run
    kind: str               # "down" | "release" | "quit" | "screenshot"
    button: str = ""
    path: str = ""          # screenshot only
    line: int = 0

    def __str__(self):
        arg = self.button or self.path
        return f"{self.t:.3f} {self.kind}{' ' + arg if arg else ''}"


class TimelineError(ValueError):
    pass


# gwemu's `press` is a tap: down, then release this long after
# (GNW_TL_TAP_NS in hw/misc/gnw_timeline.c).
TAP_S = 0.100

# `@N` addresses a VBLANK FRAME, not a time. There is no host-visible frame
# counter on hardware, so a frame-addressed timeline is converted at the
# nominal refresh -- the same 60Hz approximation gwemu itself uses for the
# release of a frame-addressed press. It is an approximation, and playback
# says so out loud rather than pretending the file was time-addressed.
FRAME_HZ = 60.0


def parse(path, frame_hz=FRAME_HZ):
    """Parse a .tl into a time-sorted event list. Raises TimelineError."""
    with open(path) as fh:
        return parse_text(fh.read(), name=path, frame_hz=frame_hz)


def _parse_time(tok):
    """`[MM:]SS[.fff][s]` -> seconds, mirroring gnw_timeline_parse_time()."""
    mins = 0.0
    if ":" in tok:
        head, tok = tok.split(":", 1)
        mins = float(head)          # ValueError -> caller reports the line
        if mins < 0:
            raise ValueError(tok)
    if tok.endswith("s"):
        tok = tok[:-1]
    secs = float(tok)
    if secs < 0 or (mins and secs >= 60):
        raise ValueError(tok)
    return mins * 60.0 + secs


def parse_text(text, name="<timeline>", frame_hz=FRAME_HZ):
    """The .tl grammar, as gwemu's own parser implements it.

    Read out of hw/misc/gnw_timeline.c rather than guessed, because the half of
    it a recording happens to use is NOT the whole language: every checked-in
    timeline except the recorded one is written in `press`/`screenshot`, which
    a parser built only from a recording rejects outright.

        [MM:]SS[.fff][s] | @FRAME    press|down|hold|release BTN[+BTN...] [DUR]
                                     screenshot PATH
                                     quit

    `press` is down plus a release TAP_S later; `hold X` the same with X
    seconds; buttons chord with `+`; names are case-insensitive and the power
    button is `pwr`.
    """
    events = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            raise TimelineError(f"{name}:{lineno}: not an event: {raw.strip()!r}")
        stamp, act = parts[0], parts[1].lower()
        if stamp.startswith("@"):
            try:
                t = int(stamp[1:]) / frame_hz
            except ValueError:
                raise TimelineError(
                    f"{name}:{lineno}: bad frame number {stamp!r}")
        else:
            try:
                t = _parse_time(stamp)
            except ValueError:
                raise TimelineError(
                    f"{name}:{lineno}: not a timestamp: {stamp!r}")

        if act == "quit":
            events.append(Event(t, "quit", line=lineno))
            continue
        if act == "screenshot":
            if len(parts) < 3:
                raise TimelineError(f"{name}:{lineno}: screenshot with no path")
            events.append(Event(t, "screenshot", path=parts[2], line=lineno))
            continue
        if act not in ("press", "down", "hold", "release"):
            raise TimelineError(
                f"{name}:{lineno}: unknown action {act!r} (expected "
                f"press/down/hold/release/screenshot/quit)")
        if len(parts) < 3:
            raise TimelineError(f"{name}:{lineno}: {act} with no button")

        buttons = []
        for word in parts[2].lower().split("+"):
            word = BUTTON_ALIASES.get(word, word)
            if word not in BUTTON_BITS:
                raise TimelineError(
                    f"{name}:{lineno}: unknown button {word!r}; known: "
                    f"{' '.join(sorted(BUTTON_BITS))}")
            buttons.append(word)

        release_after = None
        if act == "press":
            release_after = TAP_S
        elif act == "hold":
            if len(parts) < 4:
                raise TimelineError(
                    f"{name}:{lineno}: hold with no duration")
            try:
                release_after = float(parts[3])
            except ValueError:
                raise TimelineError(
                    f"{name}:{lineno}: bad hold duration {parts[3]!r}")
            if release_after <= 0:
                raise TimelineError(f"{name}:{lineno}: hold duration must be >0")
        for b in buttons:
            if act == "release":
                events.append(Event(t, "release", b, line=lineno))
            else:
                events.append(Event(t, "down", b, line=lineno))
                if release_after is not None:
                    events.append(Event(t + release_after, "release", b,
                                        line=lineno))
    # A stable sort keeps same-timestamp events in file order, which is what a
    # recording means by them.
    events.sort(key=lambda e: e.t)
    return events


def quit_time(events):
    for e in events:
        if e.kind == "quit":
            return e.t
    return None


def states(events):
    """Fold events into the sequence of (t, mask) the device should see.

    Consecutive events at the same timestamp collapse into ONE write -- two
    writes 0 ms apart are indistinguishable to the device anyway, and issuing
    them separately only spends latency.
    """
    out = []
    mask = 0
    for e in events:
        if e.kind == "quit":
            break
        if e.kind == "screenshot":      # gwemu-side only; nothing to inject
            continue
        bit = 1 << BUTTON_BITS[e.button]
        mask = (mask | bit) if e.kind == "down" else (mask & ~bit)
        if out and abs(out[-1][0] - e.t) < 1e-9:
            out[-1] = (out[-1][0], mask)
        else:
            out.append((e.t, mask))
    return out


def presses(events):
    """[(button, down_t, release_t|None)] -- what the file asks to be held."""
    open_at = {}
    out = []
    for e in events:
        if e.kind == "down":
            open_at[e.button] = e.t
        elif e.kind == "release" and e.button in open_at:
            out.append((e.button, open_at.pop(e.button), e.t))
    for b, t in open_at.items():
        out.append((b, t, None))
    out.sort(key=lambda p: p[1])
    return out


def short_presses_of(events):
    """Presses the file holds for less than one input poll (~16ms).

    Not corrected -- reported. A press this short may be delivered over SWD and
    never observed by the firmware, and a test that quietly presses nothing is
    worse than one that says so.
    """
    return [p for p in presses(events)
            if p[2] is not None and (p[2] - p[1]) * 1000 < SHORT_PRESS_MS]


@dataclass
class Delivery:
    """One state change as it was ACTUALLY delivered."""
    scheduled: float
    actual: float
    mask: int
    write_s: float = 0.0

    @property
    def lateness(self):
        return self.actual - self.scheduled


class Player:
    """Base: a timeline playback that a Target owns."""

    def start(self, t0=None):
        return self

    def stop(self):
        pass

    def join(self, timeout=None):
        pass

    @property
    def finished(self):
        return True

    def report(self):
        return ""

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class GwemuPlayer(Player):
    """Inert marker: gwemu replays the file itself, via GNW_TIMELINE.

    Nothing to drive here; it exists so callers can treat both tiers the same
    and still print where playback came from.
    """

    def __init__(self, path):
        self.path = path
        self.events = parse(path)

    def report(self):
        return (f"timeline {self.path}: {len(self.events)} events replayed by "
                f"gwemu itself (GNW_TIMELINE, emulated time -- exact)")


class ShadowWordPlayer(Player):
    """Replay a timeline onto real hardware through the REMOTE_INPUT shadow word.

    Runs on its own thread so the harness can keep polling the target while
    input is delivered -- which is exactly what gwemu's built-in playback gives
    us, and the reason the two tiers can share a test.

    The backend is shared with the harness, so writes are serialised on `lock`
    (pass the Target's lock if it has one). A write is never SKIPPED to catch
    up: dropping a state change loses a press, and a lost press is a test that
    quietly does nothing.
    """

    def __init__(self, backend, events, addr=SHADOW_ADDR, lock=None,
                 verbose=True):
        self.backend = backend
        self.events = events
        self.addr = addr
        self.lock = lock or threading.Lock()
        self.verbose = verbose
        self.states = states(events)
        self.quit_at = quit_time(events)
        self.deliveries: list[Delivery] = []
        self._thread = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self.t0 = None
        self.error = None
        self._guard_installed = False

    # -- plumbing ---------------------------------------------------------
    def _write(self, mask):
        t = time.time()
        with self.lock:
            self.backend.write_uint32(self.addr, mask & 0xFFFFFFFF)
        return t, time.time() - t

    def short_presses(self):
        return short_presses_of(self.events)

    def start(self, t0=None):
        self.t0 = t0 if t0 is not None else time.time()
        if self.verbose:
            risky = self.short_presses()
            print(f"[timeline] {len(self.states)} state changes over "
                  f"{self.states[-1][0] if self.states else 0:.1f}s "
                  f"via shadow word 0x{self.addr:08X}")
            if risky:
                print(f"[timeline] WARNING: {len(risky)} press(es) held for "
                      f"under {SHORT_PRESS_MS}ms; the device polls buttons "
                      f"once a frame and may not see them: "
                      + ", ".join(f"{b}@{d:.3f}s({(r-d)*1000:.0f}ms)"
                                  for b, d, r in risky[:6]))
        self._install_guard()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="timeline")
        self._thread.start()
        return self

    # -- THE INTERRUPTED-RUN GUARD ----------------------------------------
    #
    # The shadow word OVERRIDES the physical buttons on every input poll, and
    # it lives in D2 SRAM -- which SURVIVES RESET. So a run that dies without
    # zeroing it does not merely end: it strands the DEVICE, pinning a fake
    # button state through every later session until something clears it, and
    # the owner's own presses stop registering. That is not a hypothetical --
    # it cost a session, with the symptom looking exactly like a guest input
    # bug.
    #
    # The player's thread is a daemon, so a killed process never reaches its
    # own cleanup. atexit covers a normal exit and an uncaught exception;
    # SIGINT and SIGTERM do not run atexit at all, so they are chained
    # explicitly. Same shape as the project's temp-file rule: a finally, plus
    # a guard for the paths a finally cannot see.
    def _install_guard(self):
        if self._guard_installed:
            return
        self._guard_installed = True
        atexit.register(self._release_quietly)
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                prev = signal.getsignal(sig)
            except (ValueError, OSError):
                continue            # not the main thread: nothing to install
            def handler(signum, frame, _prev=prev):
                self._release_quietly()
                if callable(_prev):
                    _prev(signum, frame)
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass                # not the main thread

    def _release_quietly(self):
        """Zero the shadow word, best effort, from any exit path."""
        self._stop.set()
        try:
            self._write(0)
        except Exception:
            pass                    # a dead probe must not mask the real error

    def _run(self):
        try:
            # Baseline: released. Also proves the cell is writable before the
            # first real event, rather than at 5.5s into a run.
            self._write(0)
            for sched, mask in self.states:
                if self._stop.is_set():
                    break
                deadline = self.t0 + sched
                delay = deadline - time.time()
                if delay > 0:
                    if self._stop.wait(delay):
                        break
                actual, write_s = self._write(mask)
                self.deliveries.append(Delivery(sched, actual - self.t0, mask,
                                                write_s))
            # `quit` on hardware = stop injecting, hands off the device.
            if self.quit_at is not None:
                delay = self.t0 + self.quit_at - time.time()
                if delay > 0:
                    self._stop.wait(delay)
            self._write(0)
        except Exception as exc:            # a dead probe must not hang a test
            self.error = exc
        finally:
            self._done.set()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        try:
            self._write(0)                  # never leave a button held
        except Exception:
            pass

    def join(self, timeout=None):
        self._done.wait(timeout)

    @property
    def finished(self):
        return self._done.is_set()

    # -- what actually happened -------------------------------------------
    def stats(self):
        late = [d.lateness for d in self.deliveries]
        writes = [d.write_s for d in self.deliveries]
        held = self.delivered_presses()
        holds = [h[3] for h in held if h[3] is not None]
        return {
            "events": len(self.states),
            "delivered": len(self.deliveries),
            "late_mean": sum(late) / len(late) if late else 0.0,
            "late_max": max(late) if late else 0.0,
            "late_min": min(late) if late else 0.0,
            "write_mean": sum(writes) / len(writes) if writes else 0.0,
            "write_max": max(writes) if writes else 0.0,
            "hold_min": min(holds) if holds else 0.0,
            "requested_short": len(self.short_presses()),
            "error": repr(self.error) if self.error else None,
        }

    def delivered_presses(self):
        """[(button, actual_down, actual_release|None, held_s)] as delivered.

        Derived from the MASKS actually written, so it reflects the schedule
        the device saw -- not the one the file asked for.
        """
        out = []
        down_at = {}
        prev = 0
        for d in self.deliveries:
            changed = prev ^ d.mask
            for bit in range(12):
                if not changed & (1 << bit):
                    continue
                name = BIT_NAME[bit]
                if d.mask & (1 << bit):
                    down_at[name] = d.actual
                elif name in down_at:
                    t = down_at.pop(name)
                    out.append((name, t, d.actual, d.actual - t))
            prev = d.mask
        for name, t in down_at.items():
            out.append((name, t, None, None))
        out.sort(key=lambda p: p[1])
        return out

    def report(self):
        s = self.stats()
        if not s["delivered"]:
            return "timeline: nothing was delivered"
        lines = [
            f"timeline: {s['delivered']}/{s['events']} state changes delivered "
            f"over SWD",
            f"  lateness vs the file: mean {s['late_mean']*1000:.1f}ms, "
            f"max {s['late_max']*1000:.1f}ms, min {s['late_min']*1000:.1f}ms",
            f"  shadow-word write: mean {s['write_mean']*1000:.2f}ms, "
            f"max {s['write_max']*1000:.2f}ms",
            f"  shortest press actually held: {s['hold_min']*1000:.0f}ms "
            f"({s['requested_short']} press(es) in the file are shorter than "
            f"the {SHORT_PRESS_MS}ms input poll and may not have registered)",
        ]
        if s["error"]:
            lines.append(f"  PLAYBACK FAILED: {s['error']}")
        return "\n".join(lines)


def open_player(target, path, **kw):
    """The right player for whatever `target` is. None if there is no timeline.

    gwemu keeps its built-in replay (exact, emulated time); hardware gets the
    shadow-word player over the target's own backend.
    """
    if not path:
        return None
    events = parse(path)
    if getattr(target, "timeline_is_builtin", False):
        return GwemuPlayer(path)
    backend = getattr(target, "backend", None)
    if backend is None:
        raise RuntimeError("target has no backend to inject input through")
    return ShadowWordPlayer(backend, events, **kw)
