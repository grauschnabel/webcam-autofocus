"""Autofocus engine: reads the camera, focuses on the face, and passes the image on to a
virtual camera (v4l2loopback). No UI of its own — the displays (terminal, tray)
only read the state (mode, focus, ema, box, log, history, error)."""
import glob
import json
import math
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import cameras

AUTO = "focus_automatic_continuous"
SWEEP_STEP = 15                                          # step of the local search
FINE_STEP = 10                                           # the parabola takes over below this step size
CALIB_DIR = Path.home() / ".cache" / "webcam-autofocus"   # one calibration file per camera (v2: points with sharpness value)


@dataclass
class Config:
    device: str | None = None          # camera (/dev/videoN); default: last chosen, otherwise first with focus control
    out: str | None = None             # virtual camera; default: "<camera name> (Autofocus)", created if needed
    no_output: bool = False            # only control focus, no virtual camera
    width: int = 1280
    height: int = 720
    fmin: int = 1
    fmax: int = 300
    coarse: int = 20                   # step size of the coarse search
    climb: int = 40                    # initial step size when refocusing
    backlash: int = 33                 # play of the lens: commands that go downwards are this much lower
    settle: float = 0.2                # seconds to wait after a focus change
    size_tol: float = 0.15             # face width ± beyond which to refocus
    size_tol_pred: float = 0.08        # same, once a prediction is available
    dynamic: bool = False              # adapt eval_every to the CPU load automatically
    cpu_target: float = 25.0           # target: percent of one CPU core for this program (dynamic)
    recheck: float = 60.0              # seconds: when sitting still, check/improve the focus regularly (0 = off)
    verify_delay: float = 1.5          # seconds until the check after a prediction
    sharp_drop: float = 0.6            # sharpness fraction below which to refocus
    hold: float = 1.5                  # seconds this has to persist
    cooldown: float = 5.0              # lockout time after focusing
    eval_every: int = 10               # evaluate only every n-th camera frame (sharpness/face)
    freeze: bool = False               # output a still image during the search
    reset: bool = False                # ignore the learned calibration


class EngineError(Exception):
    pass


class Stopped(Exception):
    pass


def v4l2(dev, **ctrls):
    """Set V4L2 controls through v4l2-ctl; a failure becomes an EngineError that says why."""
    args = ["v4l2-ctl", "-d", dev]
    for name, value in ctrls.items():
        args += ["-c", f"{name}={value}"]
    try:
        subprocess.run(args, check=True, capture_output=True, text=True, errors="replace", timeout=5)
    except subprocess.CalledProcessError as err:
        why = (err.stderr or "").strip().splitlines()
        raise EngineError("v4l2-ctl failed: " + (why[-1] if why else f"exit status {err.returncode}"))
    except (OSError, subprocess.SubprocessError) as err:         # not installed, did not answer in time
        raise EngineError(f"v4l2-ctl failed: {err}")


def load_cascade():
    dirs = [getattr(getattr(cv2, "data", None), "haarcascades", "")]
    dirs += glob.glob("/usr/share/opencv*/haarcascades/")
    for d in dirs:
        path = os.path.join(d, "haarcascade_frontalface_default.xml")
        if d and os.path.exists(path):
            return cv2.CascadeClassifier(path)
    raise EngineError("Haar cascade not found (install the opencv-data package)")


def sharpness(frame, box):
    """Variance of the Laplacian in the core of the face box (eyes/nose/mouth)."""
    x, y, w, h = box
    x, y, w, h = x + int(w * .2), y + int(h * .2), int(w * .6), int(h * .6)
    roi = frame[max(y, 0):y + h, max(x, 0):x + w]
    if roi.size == 0:
        return 0.0
    gray = cv2.GaussianBlur(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY), (3, 3), 0)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


class AutoFocus:
    def __init__(self, cfg=None):
        self.a = cfg or Config()
        self.on_frame = None               # hook, called per camera frame (the display throttles itself)
        self.error = None
        self.lens_known = False
        self._cmd = None                   # last command sent to the lens (differs from focus after a downward move)
        self.confirm_create = False        # a UI sets this to be asked before a password dialog (see _virtual)
        self.need_virtual = False          # set when the virtual camera is missing and the UI should ask
        self._thread = None
        self._stop = threading.Event()
        self._base = (self.a.fmin, self.a.fmax, self.a.coarse, self.a.climb)   # defaults that each camera is fitted to
        self.preview_on = False            # the UI sets this while a preview is visible
        self.preview_width = 640           # desired width of the preview image in pixels
        self.preview_overlay = False       # draw the detected face frame into the preview image
        self._reset_state()

    def _pick_camera(self):
        """Pick a camera and fit the focus range/search steps to it."""
        a = self.a
        self.cam = cameras.find_camera(dev=a.device)
        fmin, fmax, coarse, climb = self._base
        if self.cam:
            a.fmin, a.fmax = max(fmin, self.cam.fmin), min(fmax, self.cam.fmax)
            scale = min(1.0, (a.fmax - a.fmin) / max(1, fmax - fmin))   # smaller range -> smaller steps
            a.coarse, a.climb = max(1, round(coarse * scale)), max(2, round(climb * scale))
        else:
            a.fmin, a.fmax, a.coarse, a.climb = fmin, fmax, coarse, climb

    def set_camera(self, cam):
        """Choose another camera (engine must be stopped); loads its calibration. The choice is
        remembered by name so the camera is found again under a new number after a USB dropout."""
        cameras.save_choice(cam.name)
        self.a.device = None
        self.a.out = None
        self._reset_state()

    @property
    def virtual_name(self):
        return self.cam.virtual_name if self.cam else None

    def _calib_file(self):
        slug = "".join(c if c.isalnum() else "-" for c in (self.cam.name if self.cam else "unknown")).lower()
        return CALIB_DIR / f"{slug}-v2.json"

    def _reset_state(self):
        self._pick_camera()
        self.focus = self.a.fmin
        self.mode = "off"
        self.log = deque(maxlen=8)
        self.history = deque(maxlen=300)   # (time, focus, sharpness), every 0.1 s
        self.changed_t = 0.0
        self.last_hist = 0.0
        self.preview = None        # (width, height, BGR bytes) of the output image, scaled down
        self.preview_t = 0.0
        self._reset_run()
        self._load_calibration()

    def _reset_run(self):
        """Forget everything about the previous run. In between, the camera's own autofocus moved
        the lens, so the old sharpness reference, face width and timers no longer fit."""
        self.box = None            # smoothed face box
        self.ref = None            # sharpness right after the last focusing
        self.ref_w = None          # face width at that time
        self.ema = None
        self.bad_since = None
        self.cooldown = 0.0
        self.lens_known, self._cmd = False, None    # the camera's own autofocus moved the lens: where it is, we do not know
        self.freeze = None         # still image during the search
        self.verify_at = None      # time of the check after a prediction
        self.recheck_at = None     # next regular check
        self.face_t = time.time()  # last time a face was seen (at the start: since when we wait for one)

    def _load_calibration(self):
        """Load the stored calibration of the current camera."""
        self.calib = {}            # face width (bucket) -> [width, focus, sharpness], learned from every search
        self.model = None          # (a, b, wmin, wmax): focus = a + b * face width
        if not self.a.reset:
            try:
                for w, f, sc in json.loads(self._calib_file().read_text())["points"]:
                    self.calib[round(w / 15)] = [w, f, sc]
                self.fit()
            except (OSError, ValueError, KeyError):
                pass

    # ---------- Calibration: face width -> focus ----------
    def fit(self):
        pts = list(self.calib.values())
        self.model = None
        if len(pts) < 2:
            return
        ws, fs = np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)
        # sharper measurements count more; never 0: polyfit with all-zero weights aborts the whole process in LAPACK
        wt = np.maximum(np.array([p[2] for p in pts], float), 1e-6)
        if ws.max() < ws.min() * 1.15:                       # too little spread for a line
            return
        b, a = np.polyfit(ws, fs, 1, w=np.sqrt(wt))
        keep = np.abs(fs - (a + b * ws)) < 40                 # drop outliers, then fit again
        if 2 <= keep.sum() < len(ws) and ws[keep].max() >= ws[keep].min() * 1.15:
            ws, fs, wt = ws[keep], fs[keep], wt[keep]
            b, a = np.polyfit(ws, fs, 1, w=np.sqrt(wt))
        if b > 0:                                            # closer (wider face) = higher value
            self.model = (a, b, ws.min(), ws.max())

    def _clamp(self, focus):
        return int(min(max(focus, self.a.fmin), self.a.fmax))

    def predict(self, w):
        if self.model is None:
            return None
        a, b, wmin, wmax = self.model
        if not wmin * .75 <= w <= wmax * 1.3:                # do not extrapolate far outside
            return None
        return self._clamp(a + b * w)

    def record(self, w, focus, score):
        """Remember a measurement point. Per width the sharper measurement wins; weaker ones only
        chip away at the old value (light can change) until a new measurement replaces it."""
        key = round(w / 15)
        old = self.calib.get(key)
        if old and score < .7 * old[2] and abs(old[1] - focus) > 5:
            old[2] *= .9
        else:
            self.calib.pop(key, None)
            self.calib[key] = [w, focus, score]
        while len(self.calib) > 12:
            self.calib.pop(next(iter(self.calib)))
        self.fit()
        try:
            CALIB_DIR.mkdir(parents=True, exist_ok=True)
            self._calib_file().write_text(json.dumps({"points": list(self.calib.values())}))
        except OSError:
            pass

    # ---------- Camera / output ----------
    def _open(self):
        a = self.a
        before = self.cam
        self._pick_camera()
        if not self.cam:
            raise EngineError("No camera with focus control found (v4l2-ctl --list-devices)")
        if self.cam.name != (before.name if before else None):   # plugged in after the start, or another one than loaded
            self._load_calibration()
        self.dev = self.cam.dev
        self.cascade = load_cascade()
        self.cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        try:
            self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
            ok, frame = self.cap.read()
            if not ok:
                raise EngineError("No image from the camera — is another program using it (e.g. Zoom)?")
            self.h, self.w = frame.shape[:2]
            self.last = frame
            self.out = None
            if not a.no_output:
                try:
                    out = a.out or self._virtual()
                except RuntimeError as err:
                    raise EngineError(str(err))
                if not os.path.exists(out):
                    raise EngineError(f"Virtual camera {out} is missing — did you run setup.sh?")
                try:
                    self.out = subprocess.Popen(
                        ["ffmpeg", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                         "-s", f"{self.w}x{self.h}", "-r", "30", "-i", "-",
                         "-pix_fmt", "yuv420p", "-f", "v4l2", out],
                        stdin=subprocess.PIPE)
                except OSError as err:
                    raise EngineError(f"ffmpeg could not be started ({err}) — is it installed?")
        except BaseException:
            self.cap.release()                               # a failed start must not keep the camera busy
            raise

    def _virtual(self):
        """The virtual camera. If it is missing and creating it would ask for a password, a UI that
        sets confirm_create gets to explain that first (need_virtual) instead of a bare password dialog."""
        dev = cameras.find_virtual(self.cam)
        if dev:
            return dev
        if self.confirm_create and cameras.needs_password():
            self.need_virtual = True
            raise EngineError("The virtual camera does not exist yet")
        self.log.append(f"{time.strftime('%H:%M:%S')}  Creating the virtual camera \"{self.cam.virtual_name}\"")
        return cameras.ensure_virtual(self.cam)

    def _close(self):
        try:
            v4l2(self.dev, **{AUTO: 1})
        except Exception:
            pass
        self.cap.release()
        if self.out:
            try:
                self.out.stdin.close()
                self.out.wait(timeout=3)
            except Exception:
                self.out.kill()

    def grab(self):
        if self._stop.is_set():
            raise Stopped
        ok, frame = self.cap.read()
        if not ok:
            raise EngineError("The camera no longer delivers an image")
        self.last = frame
        if self.out:
            try:
                self.out.stdin.write((self.freeze if self.freeze is not None else frame).tobytes())
            except BrokenPipeError:
                raise EngineError("ffmpeg/virtual camera terminated — is v4l2loopback running?")
        now = time.time()
        if self.preview_on and now - self.preview_t >= 0.1:
            self._make_preview(frame, now)
        if now - self.last_hist >= 0.1:
            self.last_hist = now
            self.history.append((now, self.focus, self.ema or 0.0))
        if self.on_frame:
            self.on_frame(self)
        return frame

    def _make_preview(self, frame, now):
        """Scale down the output image (as it would arrive in Zoom); only on request, costs nothing otherwise."""
        img = self.freeze if self.freeze is not None else frame
        s = min(1.0, self.preview_width / self.w)            # as big as the preview in the window (the UI sets preview_width)
        small = img if s == 1 else cv2.resize(img, (int(self.w * s), int(self.h * s)), interpolation=cv2.INTER_AREA)
        if self.preview_overlay and self.box:
            if s == 1:
                small = small.copy()                         # do not draw into the image that goes to the virtual camera
            x, y, w, h = (int(v * s) for v in self.box)
            color = (26, 153, 242) if self.mode == "searching focus" else (90, 179, 51)   # BGR: orange / green
            cv2.rectangle(small, (x, y), (x + w, y + h), color, max(2, int(3 * s)))
        self.preview = (small.shape[1], small.shape[0], small.tobytes())
        self.preview_t = now

    def set_focus(self, value):
        """Move the lens and wait until it has arrived. The motor needs about 1 s for 60 units and 0.3 s for
        10 (more than a second for a long jump), and it starts a moment late, so a picture that merely looks
        steady is no proof. Whatever is read earlier is the blur of the lens on its way."""
        value = self._clamp(value)
        cmd = value
        if self._cmd is not None and value < self._cmd:      # the lens has play: from above the same command is a different
            cmd = max(self.a.fmin, value - self.a.backlash)  # focus, so values are always meant as "arrived from below"
        jump = abs(cmd - self._cmd) if self._cmd is not None else self.a.fmax - self.a.fmin   # unknown after the camera's own autofocus
        v4l2(self.dev, focus_absolute=cmd)
        self.focus, self.lens_known, self._cmd = value, True, cmd
        end = time.time() + min(1.8, self.a.settle + .012 * jump)
        while time.time() < end:                 # drain the buffer meanwhile
            self.grab()

    def measure(self, box, n=3):
        """Sharpness at the current focus. Movement of the head only ever blurs, so of n frames the best two
        count (their mean); the median would follow every dip."""
        vals = sorted(sharpness(self.grab(), box) for _ in range(n))
        if n >= 5:                                           # the reference: like the running value in run(), not an optimistic one
            return float(np.median(vals))
        return float(np.mean(vals[-2:]))

    def find_face(self, frame):
        s = 480 / self.w
        gray = cv2.cvtColor(cv2.resize(frame, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY)
        faces = self.cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return tuple(int(v / s) for v in (x, y, w, h))

    def _adapt(self, now):
        """Measure own CPU load every 3 s and adjust eval_every (too high -> evaluate less often)."""
        if self.ref is None:                                 # no focus value yet: evaluate quickly
            self.a.eval_every = min(self.a.eval_every, 5)
            return
        cpu = time.process_time()
        last = getattr(self, "_cpu_mark", None)
        if last is None or now - last[0] >= 3:
            self._cpu_mark = (now, cpu)
            if last:
                load = 100 * (cpu - last[1]) / (now - last[0])
                k = int(self.a.eval_every)
                if load > self.a.cpu_target * 1.1:
                    self.a.eval_every = min(30, k + max(1, k // 5))
                elif load < self.a.cpu_target * .85 and k > 1:
                    self.a.eval_every = max(1, k - max(1, k // 5))

    # ---------- Focus search ----------
    def search(self, box, reason):
        old = self.focus
        verify = reason == "check"
        self.verify_at = None
        self.mode = "searching focus"
        if self.a.dynamic:                                   # orange = no value: evaluate quickly, afterwards by load again
            self.a.eval_every = min(self.a.eval_every, 5)
        self.freeze = self.last.copy() if self.a.freeze else None
        scores = {}

        def score(v):
            v = self._clamp(v)
            if v not in scores:
                self.set_focus(v)
                scores[v] = self.measure(box)
            return scores[v]

        def finish(top, step):
            """Peak between the measured points: vertex of a parabola through log(sharpness) of the best
            point and its two neighbours (if both were measured)."""
            lo, hi = scores.get(top - step), scores.get(top + step)
            if lo and hi and lo > 0 and hi > 0 and scores[top] > 0:
                l, m, h = math.log(lo), math.log(scores[top]), math.log(hi)
                curv = l - 2 * m + h
                if curv < 0:
                    top += max(-step, min(step, round(step * (l - h) / (2 * curv))))
            return self._clamp(top)

        def sweep(start, step, retry=True):
            """Upwards in equal steps and no turning back while measuring (the lens has play, every
            reversal would spoil the next value): stop when the picture is clearly past the peak. If the
            first point is the sharpest one, the peak is lower: once more from further down."""
            pts, v = [], self._clamp(start)
            while v <= self.a.fmax:
                score(v)
                pts.append(v)
                top = max(pts, key=scores.get)
                if len(pts) >= 3 and v - top >= step and scores[v] < .85 * scores[top]:
                    break
                v += step
            if retry and len(pts) > 1 and max(pts, key=scores.get) == pts[0] and pts[0] > self.a.fmin:
                return sweep(pts[0] - 3 * step, step, retry=False)
            return finish(max(scores, key=scores.get), step)

        def score_run_low(top):
            """The last two measurements are far below the best one."""
            last = list(scores.values())[-2:]
            return len(last) == 2 and all(x < .35 * top for x in last)

        def at(v):
            """Sharpness at v, or at the measured point closest to it (v may lie between two of them)."""
            return scores[min(scores, key=lambda k: abs(k - v))]

        def peak(v):
            """Real peak? The value must be clearly sharper than the worst one we tried."""
            return len(scores) < 3 or at(v) >= 1.3 * min(scores.values())

        known = float(np.median([p[2] for p in self.calib.values()])) if self.calib else 0   # typical earlier sharpness
        center, predicted = None, False
        if verify:                                           # check: only fine-tuning around the current value
            center = sweep(old - 2 * FINE_STEP, FINE_STEP)
            guess = self.predict(box[2])
            if not peak(center) or at(center) < .3 * known or \
                    (guess is not None and abs(center - guess) > 50 and at(center) < .6 * known):
                center = None                                # check finds no peak -> full search
        elif self.ref is not None or self.model is not None:   # search locally (after start or with a saved model)
            guess = self.predict(box[2])
            if guess is not None:                            # prediction from face width, fine-tuning only
                center, predicted = sweep(guess - 24, SWEEP_STEP), True
            elif self.ref is not None:                       # direction from size change: closer = higher value
                center = sweep(old - (24 if box[2] >= self.ref_w else 60), SWEEP_STEP)   # farther away: the peak is lower
            if center is not None and (not peak(center) or at(center) < .3 * known or
                                       (self.ref is not None and at(center) < .5 * self.ref)):
                center, predicted = None, False              # no clear peak -> local is not enough
        if center is None:                                   # initial focus/fallback: sweep the whole range coarsely
            for v in range(self.a.fmin, self.a.fmax + 1, self.a.coarse):
                score(v)
                top = max(scores.values())
                if top > 4 * min(scores.values()) and score_run_low(top):   # clearly past a real peak (not a bump in the blur): the rest is blur
                    break
            top = max(scores, key=scores.get)
            for v in (top - 10, top + 10, top + 20):
                score(v)
            center = finish(max(scores, key=scores.get), 10)
        trusted = peak(center)
        self.set_focus(center)
        self.freeze = None
        self.ref = self.measure(box, 5)
        if trusted and self.box is not None:                 # only clear peaks of a really detected face into memory
            self.record(box[2], center, self.ref)
        self.recheck_at = time.time() + self.a.recheck if self.a.recheck else None
        self.ref_w = box[2]
        self.ema = self.ref
        self.bad_since = None
        self.cooldown = time.time() + self.a.cooldown
        stamp = time.strftime('%H:%M:%S')
        if verify and center == old:
            self.log.append(f"{stamp}  Check: focus {center} is right")
        else:
            self.changed_t = time.time()
            self.log.append(f"{stamp}  Focus {old} → {center}  ({reason}{' · prediction' if predicted else ''})")
        if predicted:                                        # double-check the prediction again later
            self.verify_at = time.time() + self.a.verify_delay
        self.mode = "tracking face"

    def needs_refocus(self, box, now):
        """When to refocus? First measurement, face clearly closer/farther, or sharpness dropped."""
        if self.ref is None:
            return "Start" if self.box is not None or now - self.face_t > 3 else None   # no face yet: wait 3 s, then try anyway
        if now < self.cooldown:
            return None
        reason = None
        tol = self.a.size_tol_pred if self.model else self.a.size_tol
        if abs(box[2] / self.ref_w - 1) > tol:
            reason = "face closer/farther"
        elif self.ema < self.a.sharp_drop * self.ref:
            reason = "image blurry"
        if reason is None:
            self.bad_since = None
            return None
        self.bad_since = self.bad_since or now
        return reason if now - self.bad_since > self.a.hold else None

    # ---------- Main loop ----------
    def run(self):
        """Blocks until stop() is called or an error occurs (EngineError)."""
        self._stop.clear()
        self.error = None
        self.mode = "waiting for face"
        self._reset_run()
        self._open()
        try:
            try:
                v4l2(self.dev, **{AUTO: 0})
            except EngineError:
                pass                                         # no autofocus control (only focus_absolute): nothing to switch off
            n = 0
            while True:
                frame = self.grab()
                now = time.time()
                n += 1
                if self.a.dynamic:
                    self._adapt(now)
                k = max(1, int(self.a.eval_every))
                if n % k:                                        # just pass the frame through, do not evaluate
                    continue
                if (n // k) % max(1, round(6 / k)) == 0:     # face roughly every 6 camera frames
                    found = self.find_face(frame)
                    if found:
                        self.face_t = now
                        self.box = found if self.box is None else tuple(
                            int(.7 * o + .3 * f) for o, f in zip(self.box, found))
                if self.ref is not None and now - self.face_t > 2.5:   # face gone (head turned): hold focus, do not search again
                    self.box = None
                    self.bad_since = None
                    self.verify_at = None
                    if self.mode == "tracking face":
                        self.mode = "face lost (focus held)"
                    continue
                if self.mode.startswith("face lost"):
                    self.mode = "tracking face"
                box = self.box or (int(self.w * .35), int(self.h * .2),
                                   int(self.w * .3), int(self.h * .45))
                s = sharpness(frame, box)
                self.ema = s if self.ema is None else .9 * self.ema + .1 * s
                reason = self.needs_refocus(box, now)
                if reason is None and self.verify_at and now >= self.verify_at:
                    self.verify_at = None
                    if abs(box[2] / self.ref_w - 1) <= self.a.size_tol_pred:   # only when you sit still
                        reason = "check"
                if (reason is None and self.recheck_at and now >= self.recheck_at and now >= self.cooldown
                        and self.ref_w and abs(box[2] / self.ref_w - 1) <= self.a.size_tol_pred):
                    if self.ema < .8 * self.ref:              # only touch the lens if the picture really got softer
                        reason = "check"
                    else:
                        self.recheck_at = now + self.a.recheck
                if reason:
                    self.search(box, reason)
        except Stopped:
            pass
        finally:
            self.mode = "off"
            self._close()

    # ---------- Control from a UI (own thread) ----------
    def start(self):
        if self.active:
            return
        self.error = None
        self.need_virtual = False
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def _thread_main(self):
        try:
            self.run()
        except EngineError as e:
            self.error = str(e)
        except Exception as e:                                # make the unexpected visible instead of dying silently
            self.error = f"{type(e).__name__}: {e}"
        self.mode = "off"

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def on(self):
        """What the user asked for: running and not being stopped. The thread may need a moment to end
        (stop() waits for it), but a switch or icon must not jump back to "on" during that time."""
        return self.active and not self._stop.is_set()

    @property
    def active(self):
        """True as long as the thread runs (also while the camera is being opened)."""
        return bool(self._thread and self._thread.is_alive())
