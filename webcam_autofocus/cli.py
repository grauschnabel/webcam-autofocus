"""Terminal-Ansicht: Regler mit Fokuswert und Log der Fokusänderungen. Beenden mit Strg+C."""
import argparse
import dataclasses
import shutil
import signal
import sys
import time

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
        color = YELLOW if e.mode == "suche Fokus" else GREEN
        track = f"{DIM}{'━' * pos}{RESET}{color}●{RESET}{DIM}{'━' * (width - 1 - pos)}{RESET}"
        num = str(e.focus)
        start = min(max(pos - len(num) // 2, 0), width - len(num))
        face = f"{e.box[2]} px" if e.box else "—"
        sharp = f"{e.ema:.0f}" if e.ema is not None else "—"
        changed = f"  {YELLOW}◀ Fokus geändert{RESET}" if now - e.changed_t < 3 else ""
        lines = [
            f"Dell WB5023 Autofokus   {color}{e.mode}{RESET}",
            "",
            f"{lo:>4} {track} {hi}",
            " " * (5 + start) + f"{color}{num}{RESET}",
            "",
            f"Schärfe {sharp}   Gesicht {face}{changed}",
            *[f"{DIM}{line}{RESET}" for line in list(e.log)[-(UI_LINES - 6):]],
        ]
        lines += [""] * (UI_LINES - len(lines))
        sys.stdout.write(("\x1b[%dA" % UI_LINES if self.drawn else "") +
                         "".join("\x1b[2K" + line + "\n" for line in lines))
        sys.stdout.flush()
        self.drawn = True


def add_config_args(p):
    """Eine Option pro Config-Feld (Name, Typ und Standard stammen aus der Dataclass)."""
    for f in dataclasses.fields(Config):
        flag = "--" + f.name.replace("_", "-")
        default = f.default
        if isinstance(default, bool):
            p.add_argument(flag, action="store_true")
        elif default is None:
            p.add_argument(flag)
        else:
            p.add_argument(flag, type=type(default), default=default)


def main():
    p = argparse.ArgumentParser(description="Gesichts-Autofokus für die Dell WB5023 (Terminal-Ansicht)")
    add_config_args(p)
    args = p.parse_args()
    cfg = Config(**{f.name: getattr(args, f.name) for f in dataclasses.fields(Config)})
    engine = AutoFocus(cfg)
    engine.on_frame = Renderer()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.stdout.write("\x1b[?25l")                             # Cursor aus
    try:
        engine.run()
    except KeyboardInterrupt:
        pass
    except EngineError as err:
        sys.exit(f"\n{err}")
    finally:
        sys.stdout.write("\x1b[?25h\nAutofokus der Kamera wieder an.\n")


if __name__ == "__main__":
    main()
