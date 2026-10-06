import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from webcam_autofocus import engine
from webcam_autofocus.engine import Config
from tests.test_engine import engine_with_fake_lens, make_engine

BOX = (500, 200, 200, 240)


def tracking(tmp, ref=500.0):
    e = make_engine(tmp)
    e.ref, e.ref_w, e.ema, e.box, e.cooldown = ref, BOX[2], ref, BOX, 0.0
    return e


class AllowedBlur(unittest.TestCase):
    def test_default_and_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            self.assertEqual(Config().blur, 0.35)
            self.assertAlmostEqual(e.threshold, 325.0)
            e.a.manual = True
            self.assertIsNone(e.threshold)

    def test_a_drop_smaller_than_the_allowed_blur_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            e.ema = 340.0                                    # 32 % below the reference
            t = time.time()
            for dt in (0, 2, 10, 60):
                self.assertIsNone(e.needs_refocus(BOX, t + dt))

    def test_a_bigger_drop_triggers_only_after_hold(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            e.ema = 300.0                                    # 40 % below
            t = time.time()
            self.assertIsNone(e.needs_refocus(BOX, t))
            self.assertIsNone(e.needs_refocus(BOX, t + e.a.hold - .1))
            self.assertEqual(e.needs_refocus(BOX, t + e.a.hold + .1), "image blurry")
            e.ema = 500.0                                    # recovered: the timer starts over
            self.assertIsNone(e.needs_refocus(BOX, t + 5))
            self.assertIsNone(e.bad_since)

    def test_the_blur_setting_moves_the_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            e.ema = 400.0                                    # 20 % below
            e.a.blur = 0.15
            t = time.time()
            e.needs_refocus(BOX, t)
            self.assertEqual(e.needs_refocus(BOX, t + 2), "image blurry")

    def test_a_face_size_change_alone_does_not_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            big = (500, 200, 400, 480)                       # twice as wide, same sharpness
            t = time.time()
            for dt in (0, 2, 10):
                self.assertIsNone(e.needs_refocus(big, t + dt))

    def test_history_carries_the_threshold(self):
        with tempfile.TemporaryDirectory() as tmp:
            e = tracking(tmp)
            e.cap = mock.Mock(read=lambda: (True, np.zeros((72, 128, 3), np.uint8)))
            e.out, e.last_hist = None, 0.0
            e.grab()
            self.assertEqual(len(e.history[-1]), 4)
            self.assertAlmostEqual(e.history[-1][3], 325.0)
            e._reset_run()
            e.last_hist = 0.0
            e.grab()
            self.assertIsNone(e.history[-1][3])


class OnlyMoveWhenClearlyBetter(unittest.TestCase):
    def search(self, tmp, curve, start=100):
        e = engine_with_fake_lens(tmp)
        e.measure = lambda box, n=2: curve(e.focus)
        e.focus, e.ref, e.ref_w, e.box = start, 500.0, BOX[2], BOX
        e.search(e.box, "image blurry")
        return e, json.loads((Path(tmp) / "search.log").read_text().splitlines()[-1])

    def test_a_peak_less_than_15_percent_better_keeps_the_old_focus(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            # almost flat curve: the peak at 130 is 5 % above the old position 100
            e, entry = self.search(tmp, lambda f: 100.0 * (1 + .05 * np.exp(-((f - 130) / 15.0) ** 2)))
            self.assertEqual(e.focus, 100)
            self.assertTrue(e.kept)
            self.assertTrue(entry["kept"])
            self.assertIn("is right", e.log[-1])
            self.assertEqual(e.calib, {})                    # nothing learned from a search that kept the lens

    def test_a_clearly_sharper_peak_is_taken(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e, entry = self.search(tmp, lambda f: 100.0 * (1 + 2 * np.exp(-((f - 130) / 15.0) ** 2)))
            self.assertAlmostEqual(e.focus, 130, delta=5)
            self.assertFalse(e.kept)
            self.assertFalse(entry["kept"])

    def test_the_first_focus_always_takes_the_peak(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(engine, "CALIB_DIR", Path(tmp)):
            e = engine_with_fake_lens(tmp, best=150)
            e.measure = lambda box, n=2: 100.0 * (1 + .05 * np.exp(-((e.focus - 150) / 15.0) ** 2))   # nearly flat
            e.box = BOX
            e.search(e.box, "Start")
            self.assertAlmostEqual(e.focus, 150, delta=5)
            self.assertFalse(e.kept)


if __name__ == "__main__":
    unittest.main()
