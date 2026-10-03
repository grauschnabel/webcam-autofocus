"""Find cameras (V4L2 via sysfs) and provide the matching virtual camera "<name> (Autofocus)".

A camera qualifies if it is a video source (index 0) and supports `focus_absolute`. Virtual cameras
(v4l2loopback) do not count. The virtual camera is named like the real one plus a suffix, so you can
pick the right one in Zoom & co. without thinking; if it is missing, it is created through a small root
helper started by pkexec (the Debian package ships a polkit rule, so local users need no password)."""
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SYSFS = Path("/sys/class/video4linux")
SUFFIX = " (Autofocus)"
LABEL_MAX = 31         # v4l2loopback keeps 31 characters of a device name (card_label[32]); sysfs shows only those
RETRY_AFTER = 30       # seconds until a failed focus probe is repeated
HELPER = "/usr/lib/webcam-autofocus/create-virtual-camera"   # installed by the .deb; absent in a source checkout
POLKIT_ACTION = "io.github.webcamautofocus.create-virtual-camera"
CHOICE_FILE = Path.home() / ".config" / "webcam-autofocus" / "camera"
FOCUS_RE = re.compile(r"focus_absolute\b.*?min=(-?\d+)\s+max=(-?\d+)")


@dataclass(frozen=True)
class Camera:
    name: str          # readable name ("Dell Webcam WB5023")
    dev: str           # /dev/videoN of the video source
    fmin: int          # range of focus_absolute
    fmax: int

    @property
    def virtual_name(self):
        ascii_name = re.sub(r"[^A-Za-z0-9 ._+()-]", "", self.name).strip() or "Webcam"   # the root helper accepts only these
        # a longer name would be cut by the kernel, so find_virtual() would never see it again
        return ascii_name[:LABEL_MAX - len(SUFFIX)].rstrip() + SUFFIX


_range_cache = {}      # (dev, name) -> ((min, max) or None, valid until); v4l2-ctl runs once per camera


def _read(path):
    try:
        return path.read_text(errors="replace").strip()      # names are cut at 31 bytes, maybe in the middle of a character
    except OSError:
        return None


def _clean(raw):
    """sysfs name like "Integrated_Webcam_HD: Integrate" -> "Integrated Webcam HD"."""
    return raw.split(":")[0].replace("_", " ").strip()


def _focus_range(dev, raw):
    """(min, max) of focus_absolute, or None. A camera's answer is kept for good; a failed probe
    (device not yet accessible after plugging in, v4l2-ctl hiccup) is only remembered for a while."""
    key = (dev, raw)
    now = time.monotonic()
    cached = _range_cache.get(key)
    if cached and now < cached[1]:
        return cached[0]
    rng, valid_until = None, now + RETRY_AFTER
    try:
        res = subprocess.run(["v4l2-ctl", "-d", dev, "--list-ctrls"], capture_output=True, text=True, timeout=5)
        if res.returncode == 0:
            m = FOCUS_RE.search(res.stdout)
            rng, valid_until = ((int(m[1]), int(m[2])) if m else None), float("inf")
    except (OSError, subprocess.SubprocessError):
        pass
    _range_cache[key] = (rng, valid_until)
    return rng


def _sources():
    """(dev, raw name) of all V4L2 video sources, excluding this program's virtual cameras."""
    found = []
    for node in sorted(SYSFS.glob("video*"), key=lambda p: int(p.name[5:])):
        raw = _read(node / "name")
        if raw is None or _read(node / "index") != "0" or raw.endswith(SUFFIX):
            continue
        if not (node / "device" / "driver").exists():      # no driver link: v4l2loopback & co.
            continue
        found.append((f"/dev/{node.name}", raw))
    return found


def list_cameras():
    cams = []
    for dev, raw in _sources():
        rng = _focus_range(dev, raw)
        if rng:
            cams.append(Camera(_clean(raw), dev, *rng))
    return cams


def unsupported():
    """Names of cameras that are present but have no manual focus (nothing to control, so not offered)."""
    return [_clean(raw) for dev, raw in _sources() if not _focus_range(dev, raw)]


def find_camera(dev=None, name=None):
    """Camera by device path or name; without either, the saved choice, otherwise the first one."""
    cams = list_cameras()
    name = name or load_choice()
    for cam in cams:
        if (dev and cam.dev == dev) or (not dev and name and cam.name == name):
            return cam
    return cams[0] if cams and not dev else None


def load_choice():
    return _read(CHOICE_FILE)


def save_choice(name):
    try:
        CHOICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHOICE_FILE.write_text(name)
    except OSError:
        pass


def find_virtual(cam):
    """/dev/videoN of the virtual camera for this camera, or None."""
    for node in SYSFS.glob("video*"):
        if _read(node / "name") == cam.virtual_name:
            return f"/dev/{node.name}"
    return None


def needs_password():
    """True if creating the virtual camera will (probably) ask for a password: no helper installed
    (source checkout), or polkit does not authorize this session without asking."""
    if not Path(HELPER).exists():
        return True
    try:
        res = subprocess.run(["pkcheck", "--action-id", POLKIT_ACTION, "--process", str(os.getpid())],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return True
    return res.returncode != 0


def _create_command(cam, sudo):
    """The command that creates the virtual camera, as an argument list."""
    cmd = [HELPER, cam.virtual_name] if Path(HELPER).exists() else \
        ["v4l2loopback-ctl", "add", "-n", cam.virtual_name, "-x", "1"]   # source checkout
    return ["sudo", *cmd] if sudo else ["pkexec", *cmd]


def manual_command(cam):
    """The same thing as a line for the terminal (for people who do not want a dialog)."""
    line = shlex.join(_create_command(cam, sudo=True))
    if not Path(HELPER).exists():          # the helper also loads the module, the plain command does not
        line = "sudo modprobe v4l2loopback devices=0 exclusive_caps=1 && " + line
    return line


def ensure_virtual(cam):
    """Return the virtual camera, creating it if necessary (pkexec may ask for the password)."""
    dev = find_virtual(cam)
    if dev:
        return dev
    try:
        res = subprocess.run(_create_command(cam, sudo=False), capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as err:
        raise RuntimeError(f"Could not create the virtual camera: {err}")
    dev = find_virtual(cam)
    if res.returncode != 0 or not dev:
        why = (res.stderr or res.stdout).strip().splitlines()
        raise RuntimeError("Could not create the virtual camera"
                           + (f" ({why[-1]})" if why else " — is the v4l2loopback module loaded (setup.sh)?"))
    return dev
