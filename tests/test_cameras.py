import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from webcam_autofocus import cameras


class CameraNames(unittest.TestCase):
    def test_clean_strips_usb_suffix_and_underscores(self):
        self.assertEqual(cameras._clean("Integrated_Webcam_HD: Integrate"), "Integrated Webcam HD")
        self.assertEqual(cameras._clean("Some Webcam"), "Some Webcam")

    def test_virtual_name(self):
        self.assertEqual(cameras.Camera("Some Webcam", "/dev/video0", 0, 255).virtual_name,
                         "Some Webcam (Autofocus)")

    def test_virtual_name_is_plain_ascii(self):
        name = cameras.Camera("Kamera Übung; rm -rf", "/dev/video0", 0, 255).virtual_name
        self.assertEqual(name, "Kamera bung rm -rf (Autofocus)")
        self.assertEqual(cameras.Camera("Ü", "/dev/video0", 0, 255).virtual_name, "Webcam (Autofocus)")

    def test_virtual_name_fits_what_the_kernel_keeps(self):
        # v4l2loopback stores 31 characters; a longer name would never be found again and every start would add a device
        for name in ("Dell Webcam WB5023", "Integrated Webcam HD", "Logitech Webcam C930e",
                     "C922 Pro Stream Webcam", "HP 320 FHD Webcam: HP 320 FHD", "x" * 100):
            virtual = cameras.Camera(name, "/dev/video0", 0, 255).virtual_name
            self.assertLessEqual(len(virtual), cameras.LABEL_MAX, virtual)
            self.assertTrue(virtual.endswith(cameras.SUFFIX), virtual)
            self.assertFalse(virtual.startswith(" "), virtual)
        self.assertEqual(cameras.Camera("Dell Webcam WB5023", "/dev/video0", 0, 255).virtual_name,
                         "Dell Webcam WB5023 (Autofocus)")
        self.assertEqual(cameras.Camera("C922 Pro Stream Webcam", "/dev/video0", 0, 255).virtual_name,
                         "C922 Pro Stream Web (Autofocus)")

    def test_focus_range_is_parsed(self):
        line = "                 focus_absolute 0x009a090a (int)    : min=1 max=1000 step=1 default=1 value=181 flags=inactive"
        m = cameras.FOCUS_RE.search(line)
        self.assertEqual((int(m[1]), int(m[2])), (1, 1000))
        self.assertIsNone(cameras.FOCUS_RE.search("focus_automatic_continuous 0x009a090c (bool) : default=1 value=1"))


class CameraChoice(unittest.TestCase):
    cams = [cameras.Camera("First", "/dev/video0", 0, 255), cameras.Camera("Second", "/dev/video2", 1, 1000)]

    def find(self, **kw):
        with mock.patch.object(cameras, "list_cameras", return_value=self.cams), \
                mock.patch.object(cameras, "load_choice", return_value=kw.pop("saved", None)):
            return cameras.find_camera(**kw)

    def test_defaults_to_first(self):
        self.assertEqual(self.find().name, "First")

    def test_saved_name_wins(self):
        self.assertEqual(self.find(saved="Second").dev, "/dev/video2")

    def test_explicit_device(self):
        self.assertEqual(self.find(dev="/dev/video2").name, "Second")
        self.assertIsNone(self.find(dev="/dev/video9"))

    def test_choice_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(cameras, "CHOICE_FILE", Path(tmp) / "sub" / "camera"):
                cameras.save_choice("Second")
                self.assertEqual(cameras.load_choice(), "Second")


def make_node(root, number, name, index="0", driver=True):
    node = root / f"video{number}"
    (node / "device").mkdir(parents=True)
    (node / "name").write_bytes(name if isinstance(name, bytes) else name.encode())
    (node / "index").write_text(index)
    if driver:
        (node / "device" / "driver").mkdir()
    return node


class VirtualCamera(unittest.TestCase):
    def test_a_long_name_is_found_again_as_the_kernel_reports_it(self):
        cam = cameras.Camera("C922 Pro Stream Webcam", "/dev/video0", 0, 255)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_node(root, 0, "C922_Pro_Stream_Webcam: C922 Pro")
            make_node(root, 6, cam.virtual_name[:cameras.LABEL_MAX], driver=False)      # what sysfs shows
            with mock.patch.object(cameras, "SYSFS", root):
                self.assertEqual(cameras.find_virtual(cam), "/dev/video6")
                self.assertEqual([dev for dev, _ in cameras._sources()], ["/dev/video0"])   # not mistaken for a camera
                self.assertEqual(cameras.ensure_virtual(cam), "/dev/video6")                # nothing gets created

    def test_names_cut_inside_a_character_can_be_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_node(root, 0, "Kamera \u00dc".encode()[:-1])                          # the kernel cuts bytes
            make_node(root, 2, b"Other Cam")
            with mock.patch.object(cameras, "SYSFS", root):
                self.assertEqual([dev for dev, _ in cameras._sources()], ["/dev/video0", "/dev/video2"])


class Password(unittest.TestCase):
    cam = cameras.Camera("Test Cam", "/dev/video0", 1, 100)

    def test_command_for_the_terminal_uses_the_helper_when_installed(self):
        with tempfile.TemporaryDirectory() as d:
            helper = Path(d) / "helper"
            helper.touch()
            with mock.patch.object(cameras, "HELPER", str(helper)):
                self.assertEqual(cameras.manual_command(self.cam), f"sudo {helper} 'Test Cam (Autofocus)'")

    def test_command_for_a_source_checkout_loads_the_module_too(self):
        with mock.patch.object(cameras, "HELPER", "/nonexistent/helper"):
            cmd = cameras.manual_command(self.cam)
        self.assertTrue(cmd.startswith("sudo modprobe v4l2loopback devices=0 exclusive_caps=1 && sudo v4l2loopback-ctl add"))

    def test_a_source_checkout_always_asks(self):
        with mock.patch.object(cameras, "HELPER", "/nonexistent/helper"):
            self.assertTrue(cameras.needs_password())

    def test_polkit_decides_when_the_helper_is_installed(self):
        with tempfile.TemporaryDirectory() as d:
            helper = Path(d) / "helper"
            helper.touch()
            with mock.patch.object(cameras, "HELPER", str(helper)):
                for code, expected in ((0, False), (1, True), (2, True)):
                    with mock.patch.object(cameras.subprocess, "run", return_value=SimpleNamespace(returncode=code)):
                        self.assertEqual(cameras.needs_password(), expected)
                with mock.patch.object(cameras.subprocess, "run", side_effect=FileNotFoundError):
                    self.assertTrue(cameras.needs_password())


class FocusProbe(unittest.TestCase):
    OUT = "focus_absolute 0x009a090a (int)    : min=1 max=500 step=1 default=1 value=181\n"

    def setUp(self):
        cameras._range_cache.clear()
        self.addCleanup(cameras._range_cache.clear)

    def probe(self, run, now):
        with mock.patch.object(cameras.subprocess, "run", run), mock.patch.object(cameras.time, "monotonic", return_value=now):
            return cameras._focus_range("/dev/video0", "Cam")

    def test_an_answer_is_kept(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=self.OUT))
        self.assertEqual(self.probe(run, 0), (1, 500))
        self.assertEqual(self.probe(run, 1e6), (1, 500))
        self.assertEqual(run.call_count, 1)

    def test_a_camera_without_focus_is_kept_as_such(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout="brightness 0x00980900 (int) : min=0 max=255\n"))
        self.assertIsNone(self.probe(run, 0))
        self.assertIsNone(self.probe(run, 1e6))
        self.assertEqual(run.call_count, 1)

    def test_a_failed_probe_is_repeated(self):
        # right after plugging in, /dev/videoN may not be accessible yet; that must not hide the camera until restart
        for failure in (mock.Mock(return_value=SimpleNamespace(returncode=1, stdout="")),
                        mock.Mock(side_effect=subprocess.TimeoutExpired(["v4l2-ctl"], 5)),
                        mock.Mock(side_effect=FileNotFoundError("v4l2-ctl"))):
            cameras._range_cache.clear()
            self.assertIsNone(self.probe(failure, 0))
            self.assertIsNone(self.probe(failure, cameras.RETRY_AFTER / 2))                 # not hammering v4l2-ctl
            self.assertEqual(failure.call_count, 1)
            ok = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=self.OUT))
            self.assertEqual(self.probe(ok, cameras.RETRY_AFTER + 1), (1, 500))


if __name__ == "__main__":
    unittest.main()
