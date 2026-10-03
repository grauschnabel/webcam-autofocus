import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from webcam_autofocus import cameras, engine
from webcam_autofocus.engine import AutoFocus, Config, sharpness

ROOT = Path(__file__).resolve().parent.parent
CAM = cameras.Camera("Cam", "/dev/video0", 0, 255)


def make_engine(tmp, cam=None):
    with mock.patch.object(engine, "CALIB_DIR", Path(tmp)), \
            mock.patch.object(cameras, "find_camera", return_value=cam):
        e = AutoFocus(Config())
    return e


def engine_with_fake_lens(tmp, best=150):
    """An engine whose search runs without hardware: the sharpness depends only on the focus value."""
    e = make_engine(tmp, CAM)
    e.w, e.h = 1280, 720
    e.last = np.zeros((720, 1280, 3), np.uint8)
    e.set_focus = lambda value: setattr(e, "focus", int(min(max(value, e.a.fmin), e.a.fmax)))
    e.measure = lambda box, n=2: 1000.0 / (1 + abs(e.focus - best))
    return e


def fake_capture(ok=True):
    cap = mock.MagicMock()
    cap.read.return_value = (ok, np.zeros((720, 1280, 3), np.uint8) if ok else None)
    return cap


class Sharpness(unittest.TestCase):
    def test_sharp_image_scores_higher_than_flat(self):
        rng = np.random.default_rng(0)
        textured = rng.integers(0, 255, (200, 200, 3), dtype=np.uint8)
        flat = np.full((200, 200, 3), 128, np.uint8)
        box = (20, 20, 160, 160)
        self.assertGreater(sharpness(textured, box), 100 * max(sharpness(flat, box), 1e-6))

    def test_only_the_core_of_the_face_box_counts(self):
        noise = np.random.default_rng(0).integers(0, 255, (200, 200, 3), dtype=np.uint8)
        box = (0, 0, 200, 200)                           # the core is the inner 60 percent: 40..160
        edge_only = noise.copy()
        edge_only[40:160, 40:160] = 128
        core_only = np.full((200, 200, 3), 128, np.uint8)
        core_only[40:160, 40:160] = noise[40:160, 40:160]
        self.assertLess(sharpness(edge_only, box), 1.0)
        self.assertGreater(sharpness(core_only, box), 100)

    def test_a_smooth_gradient_is_not_sharp(self):
        ramp = np.tile(np.linspace(0, 255, 200).astype(np.uint8)[None, :, None], (200, 1, 3))   # large variance, no detail
        self.assertLess(sharpness(ramp, (0, 0, 200, 200)), 1.0)

    def test_empty_roi_is_zero(self):
        self.assertEqual(sharpness(np.zeros((10, 10, 3), np.uint8), (500, 500, 10, 10)), 0.0)


class Calibration(unittest.TestCase):
    def test_model_predicts_focus_from_face_width(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = make_engine(tmp)
            for w, f in ((100, 120), (150, 150), (200, 180), (250, 210)):
                e.record(w, f, 50.0)
            self.assertAlmostEqual(e.predict(175), 165, delta=3)
            self.assertIsNone(e.predict(900))            # no extrapolation far outside the measured range

    def test_calibration_is_stored_per_camera(self):
        a, b = cameras.Camera("Cam A", "/dev/video0", 0, 255), cameras.Camera("Cam B", "/dev/video2", 0, 255)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            ea, eb = make_engine(tmp, a), make_engine(tmp, b)
            ea.record(100, 100, 10.0)
            self.assertNotEqual(ea._calib_file(), eb._calib_file())
            self.assertTrue(ea._calib_file().exists())
            self.assertFalse(eb._calib_file().exists())

    def test_points_without_sharpness_do_not_abort_the_process(self):
        # np.polyfit with all-zero weights makes LAPACK terminate the whole process, silently with exit status 0
        code = textwrap.dedent("""
            import tempfile
            from pathlib import Path
            from unittest import mock
            from webcam_autofocus import cameras, engine
            with mock.patch.object(engine, "CALIB_DIR", Path(tempfile.mkdtemp())), \\
                    mock.patch.object(cameras, "find_camera", return_value=None):
                e = engine.AutoFocus(engine.Config())
                e.record(100, 100, 0.0)
                e.record(200, 180, 0.0)
            print("alive")
        """)
        res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
        self.assertIn("alive", res.stdout, res.stderr)

    def test_calibration_of_a_camera_that_shows_up_later_is_loaded(self):
        late = cameras.Camera("Late Cam", "/dev/video2", 0, 255)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            stored = make_engine(tmp, late)
            stored.record(100, 120, 50.0)
            stored.record(200, 180, 50.0)
            e = make_engine(tmp)                         # the app started while the camera was not there
            self.assertEqual(e.calib, {})
            cap = fake_capture(ok=False)                 # opening stops here, after the camera was picked
            with mock.patch.object(cameras, "find_camera", return_value=late), \
                    mock.patch.object(engine, "load_cascade"), \
                    mock.patch.object(engine.cv2, "VideoCapture", return_value=cap):
                self.assertRaises(engine.EngineError, e._open)
            self.assertEqual(len(e.calib), 2)
            self.assertIsNotNone(e.model)
            cap.release.assert_called_once()             # a failed start must not keep the camera busy

    def test_preview_settings_survive_a_camera_switch(self):
        other = cameras.Camera("Other", "/dev/video2", 0, 255)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = make_engine(tmp, CAM)
            e.preview_overlay, e.preview_width = True, 1000          # set by the window, which does not know about the switch
            with mock.patch.object(cameras, "save_choice"), mock.patch.object(cameras, "find_camera", return_value=other):
                e.set_camera(other)
            self.assertEqual((e.preview_overlay, e.preview_width), (True, 1000))


class Search(unittest.TestCase):
    def test_fallback_box_is_not_remembered(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = engine_with_fake_lens(tmp)
            e.search((448, 144, 384, 324), "Start")      # no face was found: the search used the centred default box
            self.assertEqual(e.calib, {})
            self.assertEqual(list(Path(tmp).glob("*.json")), [])
            e._reset_run()
            e.box = (500, 200, 200, 240)
            e.search(e.box, "Start")
            self.assertEqual([p[0] for p in e.calib.values()], [200])

    def test_search_finds_the_sharpest_focus(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = engine_with_fake_lens(tmp, best=150)
            e.box = (500, 200, 200, 240)
            e.search(e.box, "Start")
            self.assertAlmostEqual(e.focus, 150, delta=4)
            self.assertEqual(e.mode, "tracking face")


    def test_a_wrong_first_direction_turns_around_and_the_lens_does_not_jump_about(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = engine_with_fake_lens(tmp, best=150)
            e.focus, e.ref, e.ref_w = 100, 500.0, 400       # face got smaller -> first tries lower values
            e.box = (500, 200, 200, 240)
            visited = []
            move = e.set_focus
            e.set_focus = lambda v: (move(v), visited.append(e.focus))
            e.search(e.box, "face closer/farther")
            self.assertAlmostEqual(e.focus, 150, delta=5)
            self.assertLess(len(visited), 20)               # a handful of steps, no sweep of the whole range
            self.assertGreater(min(visited), 40)            # and it did not run off in the wrong direction


class Run(unittest.TestCase):
    def test_waiting_for_a_face_starts_with_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp)
            e.face_t = time.time() - 600                 # engine created long ago, switched on just now
            e._reset_run()
            now = time.time()
            self.assertIsNone(e.needs_refocus((0, 0, 100, 100), now + 1))
            self.assertEqual(e.needs_refocus((0, 0, 100, 100), now + 4), "Start")
            e.box = (0, 0, 100, 100)                     # a face: no waiting
            self.assertEqual(e.needs_refocus(e.box, now + 1), "Start")

    def test_run_forgets_the_previous_run_and_copes_with_a_camera_without_auto_focus(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp, CAM)
            e.ref, e.ref_w, e.ema, e.box = 5.0, 200, 5.0, (1, 2, 3, 4)
            e.bad_since, e.verify_at, e.recheck_at, e.freeze = 1.0, 1.0, 1.0, np.zeros((2, 2, 3), np.uint8)
            seen = {}

            def first_frame():
                seen.update(ref=e.ref, ema=e.ema, box=e.box, verify_at=e.verify_at, recheck_at=e.recheck_at,
                            bad_since=e.bad_since, freeze=e.freeze)
                raise engine.Stopped

            e._open = lambda: setattr(e, "dev", CAM.dev)
            e._close = lambda: None
            e.grab = first_frame
            with mock.patch.object(engine, "v4l2", side_effect=engine.EngineError("unknown control")):
                e.run()                                  # switching AUTO off fails: that must not end the run
            self.assertEqual(seen, dict.fromkeys(("ref", "ema", "box", "verify_at", "recheck_at", "bad_since", "freeze")))
            self.assertIsNone(e.error)

    def test_missing_ffmpeg_is_reported_and_releases_the_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp, CAM)
            e.a.out = "/dev/null"
            cap = fake_capture()
            with mock.patch.object(cameras, "find_camera", return_value=CAM), \
                    mock.patch.object(engine, "load_cascade"), \
                    mock.patch.object(engine.cv2, "VideoCapture", return_value=cap), \
                    mock.patch.object(engine.subprocess, "Popen", side_effect=FileNotFoundError(2, "No such file", "ffmpeg")):
                with self.assertRaisesRegex(engine.EngineError, "ffmpeg"):
                    e._open()
            cap.release.assert_called_once()


class MissingVirtualCamera(unittest.TestCase):
    def test_a_ui_that_asks_first_gets_told_instead_of_a_password_dialog(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp, CAM)
            e.confirm_create = True
            with mock.patch.object(cameras, "find_virtual", return_value=None), \
                    mock.patch.object(cameras, "needs_password", return_value=True), \
                    mock.patch.object(cameras, "ensure_virtual") as create:
                with self.assertRaises(engine.EngineError):
                    e._virtual()
            self.assertTrue(e.need_virtual)
            create.assert_not_called()

    def test_without_a_password_or_without_a_ui_it_is_created_right_away(self):
        with tempfile.TemporaryDirectory() as tmp:
            for confirm, password in ((True, False), (False, True)):
                e = make_engine(tmp, CAM)
                e.confirm_create = confirm
                with mock.patch.object(cameras, "find_virtual", return_value=None), \
                        mock.patch.object(cameras, "needs_password", return_value=password), \
                        mock.patch.object(cameras, "ensure_virtual", return_value="/dev/video9") as create:
                    self.assertEqual(e._virtual(), "/dev/video9")
                create.assert_called_once()
                self.assertFalse(e.need_virtual)


class OnOff(unittest.TestCase):
    def test_a_switch_does_not_jump_back_while_the_engine_is_shutting_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp, CAM)
            release = threading.Event()
            e._thread = threading.Thread(target=release.wait)
            e._thread.start()
            try:
                self.assertTrue(e.on)
                e._stop.set()                                # stop() was requested, the thread is still ending
                self.assertTrue(e.active)
                self.assertFalse(e.on)
            finally:
                release.set()
                e._thread.join()
            self.assertFalse(e.on)


class V4l2(unittest.TestCase):
    def test_failure_says_why(self):
        denied = subprocess.CalledProcessError(1, ["v4l2-ctl"], stderr="VIDIOC_S_CTRL: failed: Permission denied\n")
        with mock.patch.object(engine.subprocess, "run", side_effect=denied):
            with self.assertRaisesRegex(engine.EngineError, "Permission denied"):
                engine.v4l2("/dev/video0", focus_absolute=5)
        missing = FileNotFoundError(2, "No such file or directory", "v4l2-ctl")
        with mock.patch.object(engine.subprocess, "run", side_effect=missing):
            with self.assertRaisesRegex(engine.EngineError, "v4l2-ctl"):
                engine.v4l2("/dev/video0", focus_absolute=5)

    def test_does_not_wait_forever(self):
        with mock.patch.object(engine.subprocess, "run") as run:
            engine.v4l2("/dev/video0", focus_absolute=5)
        self.assertGreater(run.call_args.kwargs.get("timeout") or 0, 0)      # a number of seconds, not None
        with mock.patch.object(engine.subprocess, "run", side_effect=subprocess.TimeoutExpired(["v4l2-ctl"], 5)):
            self.assertRaises(engine.EngineError, engine.v4l2, "/dev/video0", focus_absolute=5)


class FocusRange(unittest.TestCase):
    def test_range_and_steps_follow_the_camera(self):
        small = cameras.Camera("Cam", "/dev/video0", 0, 150)
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp, small)
        self.assertEqual((e.a.fmin, e.a.fmax), (1, 150))
        self.assertLess(e.a.coarse, 30)

    def test_defaults_without_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = make_engine(tmp)
        self.assertEqual((e.a.fmin, e.a.fmax, e.a.coarse), (1, 300, 30))


if __name__ == "__main__":
    unittest.main()
