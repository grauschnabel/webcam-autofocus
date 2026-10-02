import subprocess
import tempfile
import unittest
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent / "packaging" / "create-virtual-camera"


class CreateVirtualCamera(unittest.TestCase):
    """The helper runs as root (pkexec, no password), so it may create nothing but "<name> (Autofocus)"
    devices. modprobe and v4l2loopback-ctl are stubs and PATH holds nothing else: nothing privileged happens."""

    def run_helper(self, name):
        with tempfile.TemporaryDirectory() as tmp:
            for tool, body in (("modprobe", "exit 0"), ("v4l2loopback-ctl", 'echo "$@"')):
                stub = Path(tmp, tool)
                stub.write_text(f"#!/bin/sh\n{body}\n")
                stub.chmod(0o755)
            return subprocess.run(["/bin/sh", str(HELPER), name], capture_output=True, text=True, env={"PATH": tmp})

    def test_creates_the_device_for_a_valid_name(self):
        res = self.run_helper("Dell Webcam WB5023 (Autofocus)")
        self.assertEqual((res.returncode, res.stdout), (0, "add -n Dell Webcam WB5023 (Autofocus) -x 1\n"), res.stderr)

    def test_refuses_everything_else(self):
        for name in ("", "Cam", "Cam (Autofocus", "x; id (Autofocus)", "$(id) (Autofocus)", "Kamera Ü (Autofocus)",
                     "evil\nline (Autofocus)", "\n (Autofocus)"):      # a newline must not hide behind a harmless line
            with self.subTest(name=name):
                res = self.run_helper(name)
                self.assertEqual((res.returncode, res.stdout), (2, ""))

    def test_length_limit_is_what_the_kernel_keeps(self):
        self.assertEqual(self.run_helper("A" * 19 + " (Autofocus)").returncode, 0)      # 31 characters
        self.assertEqual(self.run_helper("A" * 20 + " (Autofocus)").returncode, 2)      # 32: the kernel would cut it off


if __name__ == "__main__":
    unittest.main()
