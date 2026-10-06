"""Terminal view: slider with the focus value and a log of focus changes. Quit with Ctrl+C."""
import argparse
import dataclasses
import shutil
import signal
import sys
import time

from . import __version__
from .engine import AutoFocus, Config, EngineError

UI_LINES = 11
RESET, GREEN, YELLOW, DIM = "\x1b[0m", "\x1b[32m", "\x1b[33m", "\x1b[2m"


class Renderer:
    def __init__(self):
        self.last_draw = 0.0
        self.drawn = False

    def __call__(self, e):
        now = time.time()
        if now - self.last_draw < 0.1:
            return
        self.last_draw = now
        cols = shutil.get_terminal_size().columns
        width = max(20, min(60, cols - 14))
        lo, hi = e.a.fmin, e.a.fmax
        pos = min(max(round((e.focus - lo) / (hi - lo) * (width - 1)), 0), width - 1)
        if e.a.manual:                                            # sharp / not sharp / cannot tell yet
            color = {True: GREEN, False: YELLOW}.get(e.sharp, RESET)
        else:
            color = YELLOW if e.mode == "searching focus" else GREEN
        track = f"{DIM}{'━' * pos}{RESET}{color}●{RESET}{DIM}{'━' * (width - 1 - pos)}{RESET}"
        num = str(e.focus)
        start = min(max(pos - len(num) // 2, 0), width - len(num))
        face = f"{e.box[2]} px" if e.box else "—"
        sharp = f"{e.ema:.0f}" if e.ema is not None else "—"
        changed = f"  {YELLOW}◀ focus changed{RESET}" if now - e.changed_t < 3 else ""
        lines = [
            f"{e.cam.name if e.cam else 'Webcam'} autofocus   {color}{e.mode}{RESET}",
            "",
            f"{lo:>4} {track} {hi}",
            " " * (5 + start) + f"{color}{num}{RESET}",
            "",
            f"Sharpness {sharp}   Face {face}{changed}",
            *[f"{DIM}{line}{RESET}" for line in list(e.log)[-(UI_LINES - 6):]],
        ]
        lines += [""] * (UI_LINES - len(lines))
        sys.stdout.write(("\x1b[%dA" % UI_LINES if self.drawn else "") +
                         "".join("\x1b[2K" + line + "\n" for line in lines))
        sys.stdout.flush()
        self.drawn = True


def add_config_args(p):
    """One option per Config field (name, type and default come from the dataclass)."""
    for f in dataclasses.fields(Config):
        flag = "--" + f.name.replace("_", "-")
        default = f.default
        if isinstance(default, bool):
            p.add_argument(flag, action="store_true")
        elif default is None:
            p.add_argument(flag)
        else:
            p.add_argument(flag, type=type(default), default=default)


def config_from_args(args):
    """The Config for the options parsed after add_config_args()."""
    return Config(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Config)})


def main():
    p = argparse.ArgumentParser(description="Face autofocus for webcams with manual focus (terminal view)")
    add_config_args(p)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = p.parse_args()
    engine = AutoFocus(config_from_args(args))
    engine.on_frame = Renderer()
    for sig in (signal.SIGTERM, signal.SIGHUP):               # kill / closed terminal: still hand the camera back
        signal.signal(sig, lambda *_: sys.exit(0))
    sys.stdout.write("\x1b[?25l")                             # hide cursor
    try:
        engine.run()
    except KeyboardInterrupt:
        pass
    except EngineError as err:
        sys.exit(f"\n{err}")
    finally:
        sys.stdout.write("\x1b[?25h\nCamera autofocus back on.\n")


if __name__ == "__main__":
    main()
