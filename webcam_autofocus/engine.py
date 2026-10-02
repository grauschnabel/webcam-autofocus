"""Autofokus-Engine: liest die Kamera, fokussiert auf das Gesicht, gibt das Bild an eine
virtuelle Kamera (v4l2loopback) aus. Keine Oberfläche — die Anzeigen (Terminal, Tray)
lesen nur den Zustand (mode, focus, ema, box, log, history, error)."""
import glob
import json
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

AUTO = "focus_automatic_continuous"
CALIB_FILE = Path.home() / ".cache" / "webcam-autofocus-v2.json"   # v2: Punkte mit Schärfewert (alte Datei war verrauscht)


@dataclass
class Config:
    device: str | None = None          # Standard: Dell WB5023 per /dev/v4l/by-id
    out: str = "/dev/video10"          # virtuelle Kamera (v4l2loopback)
    no_output: bool = False            # nur Fokus steuern, keine virtuelle Kamera
    width: int = 1280
    height: int = 720
    fmin: int = 1
    fmax: int = 300
    coarse: int = 30                   # Schrittweite der Grobsuche
    climb: int = 40                    # Start-Schrittweite beim Nachfokussieren
    settle: float = 0.2                # Sekunden Wartezeit nach Fokusänderung
    size_tol: float = 0.15             # Gesichtsbreite ±, ab der neu fokussiert wird
    size_tol_pred: float = 0.08        # dasselbe, sobald eine Vorhersage möglich ist
    dynamic: bool = False              # eval_every automatisch an die CPU-Last anpassen
    cpu_target: float = 25.0           # Ziel: Prozent eines CPU-Kerns für dieses Programm (dynamisch)
    recheck: float = 60.0              # Sekunden: bei ruhigem Sitzen den Fokus regelmäßig kontrollieren/verbessern (0 = aus)
    verify_delay: float = 1.5          # Sekunden bis zur Kontrolle nach einer Vorhersage
    sharp_drop: float = 0.6            # Schärfe-Anteil, unter dem neu fokussiert wird
    hold: float = 1.5                  # Sekunden, die das anhalten muss
    cooldown: float = 5.0              # Sperrzeit nach dem Fokussieren
    eval_every: int = 10               # nur jedes x-te Kamerabild auswerten (Schärfe/Gesicht)
    freeze: bool = False               # während der Suche Standbild ausgeben
    reset: bool = False                # gelernte Kalibrierung ignorieren


class EngineError(Exception):
    pass


class Stopped(Exception):
    pass


def find_device():
    hits = sorted(glob.glob("/dev/v4l/by-id/*Dell_Webcam_WB5023*-video-index0"))
    return os.path.realpath(hits[0]) if hits else "/dev/video2"


def v4l2(dev, **ctrls):
    args = ["v4l2-ctl", "-d", dev]
    for name, value in ctrls.items():
        args += ["-c", f"{name}={value}"]
    subprocess.run(args, check=True, capture_output=True)


def load_cascade():
    dirs = [getattr(getattr(cv2, "data", None), "haarcascades", "")]
    dirs += glob.glob("/usr/share/opencv*/haarcascades/")
    for d in dirs:
        path = os.path.join(d, "haarcascade_frontalface_default.xml")
        if d and os.path.exists(path):
            return cv2.CascadeClassifier(path)
    raise EngineError("Haar-Cascade nicht gefunden (Paket opencv-data installieren)")


def sharpness(frame, box):
    """Varianz des Laplace-Operators im Kern der Gesichtsbox (Augen/Nase/Mund)."""
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
        self.on_frame = None               # Hook, pro Kamerabild aufgerufen (Anzeige drosselt selbst)
        self.running = False
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        self._reset_state()

    def _reset_state(self):
        a = self.a
        self.focus = a.fmin
        self.box = None            # geglättete Gesichtsbox
        self.ref = None            # Schärfe direkt nach dem letzten Fokussieren
        self.ref_w = None          # Gesichtsbreite dabei
        self.ema = None
        self.bad_since = None
        self.cooldown = 0.0
        self.freeze = None         # Standbild während der Suche
        self.mode = "aus"
        self.log = deque(maxlen=8)
        self.history = deque(maxlen=300)   # (Zeit, Fokus, Schärfe), alle 0,1 s
        self.changed_t = 0.0
        self.t0 = time.time()
        self.verify_at = None      # Zeitpunkt der Kontrolle nach einer Vorhersage
        self.last_hist = 0.0
        self.preview_on = False    # Oberfläche setzt das, solange eine Vorschau sichtbar ist
        self.preview_overlay = False   # gefundenen Gesichtsrahmen ins Vorschaubild zeichnen
        self.preview = None        # (Breite, Höhe, BGR-Bytes) des Ausgabebilds, verkleinert
        self.preview_t = 0.0
        self.calib = {}            # Gesichtsbreite (Bucket) -> [Breite, Fokus, Schärfe], gelernt aus jeder Suche
        self.recheck_at = None     # nächste regelmäßige Kontrolle
        self.model = None          # (a, b, wmin, wmax): Fokus = a + b * Gesichtsbreite
        if not a.reset:
            try:
                for w, f, sc in json.loads(CALIB_FILE.read_text())["points"]:
                    self.calib[round(w / 15)] = [w, f, sc]
                self.fit()
            except (OSError, ValueError, KeyError):
                pass

    # ---------- Kalibrierung: Gesichtsbreite -> Fokus ----------
    def fit(self):
        pts = list(self.calib.values())
        self.model = None
        if len(pts) < 2:
            return
        ws, fs = np.array([p[0] for p in pts], float), np.array([p[1] for p in pts], float)
        wt = np.array([p[2] for p in pts], float)             # schärfere Messungen zählen mehr
        if ws.max() < ws.min() * 1.15:                       # zu wenig Spreizung für eine Gerade
            return
        b, a = np.polyfit(ws, fs, 1, w=np.sqrt(wt))
        keep = np.abs(fs - (a + b * ws)) < 40                 # Ausreißer raus, dann neu anpassen
        if 2 <= keep.sum() < len(ws) and ws[keep].max() >= ws[keep].min() * 1.15:
            ws, fs, wt = ws[keep], fs[keep], wt[keep]
            b, a = np.polyfit(ws, fs, 1, w=np.sqrt(wt))
        if b > 0:                                            # näher (breiteres Gesicht) = höherer Wert
            self.model = (a, b, ws.min(), ws.max())

    def predict(self, w):
        if self.model is None:
            return None
        a, b, wmin, wmax = self.model
        if not wmin * .75 <= w <= wmax * 1.3:                # nicht weit außerhalb extrapolieren
            return None
        return int(min(max(a + b * w, self.a.fmin), self.a.fmax))

    def record(self, w, focus, score):
        """Messpunkt merken. Pro Breite gewinnt die schärfere Messung; schwächere nagen den alten
        Wert nur an (Licht kann sich ändern), bis eine neue Messung ihn ablöst."""
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
            CALIB_FILE.parent.mkdir(parents=True, exist_ok=True)
            CALIB_FILE.write_text(json.dumps({"points": list(self.calib.values())}))
        except OSError:
            pass

    # ---------- Kamera / Ausgabe ----------
    def _open(self):
        a = self.a
        self.dev = a.device or find_device()
        self.cascade = load_cascade()
        self.cap = cv2.VideoCapture(self.dev, cv2.CAP_V4L2)
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, a.width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, a.height)
        ok, frame = self.cap.read()
        if not ok:
            self.cap.release()
            raise EngineError("Kein Bild von der Kamera — nutzt ein anderes Programm sie gerade (z. B. Zoom)?")
        self.h, self.w = frame.shape[:2]
        self.last = frame
        self.out = None
        if not a.no_output:
            if not os.path.exists(a.out):
                self.cap.release()
                raise EngineError(f"Virtuelle Kamera {a.out} fehlt — setup.sh ausgeführt?")
            self.out = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
                 "-s", f"{self.w}x{self.h}", "-r", "30", "-i", "-",
                 "-pix_fmt", "yuv420p", "-f", "v4l2", a.out],
                stdin=subprocess.PIPE)

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
            raise EngineError("Kamera liefert kein Bild mehr")
        self.last = frame
        if self.out:
            try:
                self.out.stdin.write((self.freeze if self.freeze is not None else frame).tobytes())
            except BrokenPipeError:
                raise EngineError("ffmpeg/virtuelle Kamera beendet — läuft v4l2loopback?")
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
        """Ausgabebild (wie es in Zoom ankäme) verkleinern; nur auf Anfrage, kostet sonst nichts."""
        img = self.freeze if self.freeze is not None else frame
        s = min(1.0, 1280 / self.w)                          # volle Auflösung (max. 1280 px), damit ein großes Fenster scharf bleibt
        small = img if s == 1 else cv2.resize(img, (int(self.w * s), int(self.h * s)), interpolation=cv2.INTER_AREA)
        if self.preview_overlay and self.box:
            small = small.copy()
        if self.preview_overlay and self.box:
            x, y, w, h = (int(v * s) for v in self.box)
            color = (26, 153, 242) if self.mode == "suche Fokus" else (90, 179, 51)   # BGR: orange / grün
            cv2.rectangle(small, (x, y), (x + w, y + h), color, max(2, int(3 * s)))
        self.preview = (small.shape[1], small.shape[0], small.tobytes())
        self.preview_t = now

    def set_focus(self, value):
        value = int(min(max(value, self.a.fmin), self.a.fmax))
        v4l2(self.dev, focus_absolute=value)
        self.focus = value
        end = time.time() + self.a.settle      # Fokusmotor laufen lassen, Puffer leeren
        while time.time() < end:
            self.grab()

    def measure(self, box, n=2):
        return float(np.median([sharpness(self.grab(), box) for _ in range(n)]))

    def find_face(self, frame):
        s = 480 / self.w
        gray = cv2.cvtColor(cv2.resize(frame, None, fx=s, fy=s), cv2.COLOR_BGR2GRAY)
        faces = self.cascade.detectMultiScale(gray, 1.1, 5, minSize=(40, 40))
        if len(faces) == 0:
            return None
        x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
        return tuple(int(v / s) for v in (x, y, w, h))

    def _adapt(self, now):
        """Alle 3 s die eigene CPU-Last messen und eval_every nachführen (zu hoch -> seltener auswerten)."""
        if self.ref is None:                                 # noch kein Fokuswert: zügig auswerten
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

    # ---------- Fokussuche ----------
    def search(self, box, reason):
        old = self.focus
        verify = reason == "Kontrolle"
        self.verify_at = None
        self.mode = "suche Fokus"
        if self.a.dynamic:                                   # orange = ohne Wert: schnell auswerten, danach wieder nach Last
            self.a.eval_every = min(self.a.eval_every, 5)
        self.freeze = self.last.copy() if self.a.freeze else None
        scores = {}

        def score(v):
            v = int(min(max(v, self.a.fmin), self.a.fmax))
            if v not in scores:
                self.set_focus(v)
                scores[v] = self.measure(box)
            return scores[v]

        def refine(center, step):
            while step >= 4:                                 # Schritte halbieren
                score(center - step)
                score(center + step)
                center = max(scores, key=scores.get)
                step //= 2
            return center

        def climb(start, step, first_dir=1):
            """Bergsteigen: vom Startwert in die bessere Richtung, solange es besser wird."""
            best = start
            score(best)
            while step >= 5:
                for direction in (first_dir, -first_dir):
                    cand = best + direction * step
                    if self.a.fmin <= cand <= self.a.fmax and score(cand) > scores[best] * 1.02:
                        best = cand
                        while self.a.fmin <= best + direction * step <= self.a.fmax and \
                                score(best + direction * step) > scores[best] * 1.02:
                            best += direction * step
                        break
                step //= 2
            return best

        def peak(v):
            """Echter Gipfel? Der Wert muss deutlich schärfer sein als das Schlechteste, das wir probiert haben."""
            return len(scores) < 3 or scores[v] >= 1.3 * min(scores.values())

        center, predicted = None, False
        if verify:                                           # Kontrolle: nur Feintuning um den aktuellen Wert
            center = climb(old, 10)
            guess = self.predict(box[2])
            known = float(np.median([p[2] for p in self.calib.values()])) if self.calib else 0
            if not peak(center) or scores[center] < .3 * known or \
                    (guess is not None and abs(center - guess) > 50 and scores[center] < .6 * known):
                center = None                                # Kontrolle findet keinen Gipfel -> Gesamtsuche
        elif self.ref is not None or self.model is not None:   # lokal suchen (nach dem Start oder mit gespeichertem Modell)
            guess = self.predict(box[2])
            if guess is not None:                            # Vorhersage aus Gesichtsbreite, nur Feintuning
                center, predicted = climb(guess, 10), True
            elif self.ref is not None:                       # Richtung aus Größenänderung: näher = höherer Wert
                center = climb(old, self.a.climb, 1 if box[2] >= self.ref_w else -1)
            known = float(np.median([p[2] for p in self.calib.values()])) if self.calib else 0
            if center is not None and (not peak(center) or scores[center] < .3 * known or
                                       (self.ref is not None and scores[center] < .5 * self.ref)):
                center, predicted = None, False              # kein klarer Gipfel -> lokal reicht nicht
        if center is None:                                   # Erstfokus/Notfall: Gesamtbereich grob abfahren
            for v in range(self.a.fmin, self.a.fmax + 1, self.a.coarse):
                score(v)
            center = refine(max(scores, key=scores.get), self.a.coarse // 2)
        trusted = peak(center)
        self.set_focus(center)
        self.freeze = None
        self.ref = self.measure(box, 8)
        if trusted:                                          # nur klare Gipfel ins Gedächtnis
            self.record(box[2], center, self.ref)
        self.recheck_at = time.time() + self.a.recheck if self.a.recheck else None
        self.ref_w = box[2]
        self.ema = self.ref
        self.bad_since = None
        self.cooldown = time.time() + self.a.cooldown
        stamp = time.strftime('%H:%M:%S')
        if verify and center == old:
            self.log.append(f"{stamp}  Kontrolle: Fokus {center} stimmt")
        else:
            self.changed_t = time.time()
            self.log.append(f"{stamp}  Fokus {old} → {center}  ({reason}{' · Vorhersage' if predicted else ''})")
        if predicted:                                        # Vorhersage später noch einmal gegenprüfen
            self.verify_at = time.time() + self.a.verify_delay
        self.mode = "Gesicht verfolgt"

    def needs_refocus(self, box, now):
        """Wann neu fokussieren? Erste Messung, Gesicht deutlich näher/weiter, oder Schärfe eingebrochen."""
        if self.ref is None:
            return "Start" if self.box is not None or now - self.t0 > 3 else None
        if now < self.cooldown:
            return None
        reason = None
        tol = self.a.size_tol_pred if self.model else self.a.size_tol
        if abs(box[2] / self.ref_w - 1) > tol:
            reason = "Gesicht näher/weiter"
        elif self.ema < self.a.sharp_drop * self.ref:
            reason = "Bild unscharf"
        if reason is None:
            self.bad_since = None
            return None
        self.bad_since = self.bad_since or now
        return reason if now - self.bad_since > self.a.hold else None

    # ---------- Hauptschleife ----------
    def run(self):
        """Blockiert, bis stop() aufgerufen wird oder ein Fehler auftritt (EngineError)."""
        self._stop.clear()
        self.error = None
        self.mode = "warte auf Gesicht"
        self.face_t = time.time()
        self._open()
        self.running = True
        try:
            v4l2(self.dev, **{AUTO: 0})
            n = 0
            while True:
                frame = self.grab()
                now = time.time()
                n += 1
                if self.a.dynamic:
                    self._adapt(now)
                k = max(1, int(self.a.eval_every))
                if n % k:                                        # Bild nur durchreichen, nicht auswerten
                    continue
                if (n // k) % max(1, round(6 / k)) == 0:     # Gesicht ca. alle 6 Kamerabilder
                    found = self.find_face(frame)
                    if found:
                        self.face_t = now
                        self.box = found if self.box is None else tuple(
                            int(.7 * o + .3 * f) for o, f in zip(self.box, found))
                if self.ref is not None and now - self.face_t > 2.5:   # Gesicht weg (Kopf gedreht): Fokus halten, nichts neu suchen
                    self.box = None
                    self.bad_since = None
                    self.verify_at = None
                    if self.mode == "Gesicht verfolgt":
                        self.mode = "Gesicht verloren (Fokus bleibt)"
                    continue
                if self.mode.startswith("Gesicht verloren"):
                    self.mode = "Gesicht verfolgt"
                box = self.box or (int(self.w * .35), int(self.h * .2),
                                   int(self.w * .3), int(self.h * .45))
                s = sharpness(frame, box)
                self.ema = s if self.ema is None else .9 * self.ema + .1 * s
                reason = self.needs_refocus(box, now)
                if reason is None and self.verify_at and now >= self.verify_at:
                    self.verify_at = None
                    if abs(box[2] / self.ref_w - 1) <= self.a.size_tol_pred:   # nur wenn du ruhig sitzt
                        reason = "Kontrolle"
                if (reason is None and self.recheck_at and now >= self.recheck_at and now >= self.cooldown
                        and self.ref_w and abs(box[2] / self.ref_w - 1) <= self.a.size_tol_pred):
                    reason = "Kontrolle"                      # regelmäßig prüfen und verbessern
                if reason:
                    self.search(box, reason)
        except Stopped:
            pass
        finally:
            self.running = False
            self.mode = "aus"
            self._close()

    # ---------- Steuerung aus einer Oberfläche (eigener Thread) ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.error = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def _thread_main(self):
        try:
            self.run()
        except EngineError as e:
            self.error = str(e)
        except Exception as e:                                # Unerwartetes sichtbar machen statt still sterben
            self.error = f"{type(e).__name__}: {e}"
        self.running = False
        self.mode = "aus"

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def active(self):
        """True, solange der Thread läuft (auch während des Kamera-Öffnens)."""
        return bool(self._thread and self._thread.is_alive())
