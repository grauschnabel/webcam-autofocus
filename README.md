# webcam-autofocus

[![CI](https://github.com/grauschnabel/webcam-autofocus/actions/workflows/ci.yml/badge.svg)](https://github.com/grauschnabel/webcam-autofocus/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/grauschnabel/webcam-autofocus)](https://github.com/grauschnabel/webcam-autofocus/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**Face-tracking autofocus for Linux webcams.** Does your webcam focus on the
wall behind you instead of your face, or hunt back and forth during video calls
in Zoom, Teams or Google Meet? This tool keeps the picture sharp **on your face**
and sends the result to a virtual camera you pick in your video-call program.

## Why

Many webcams have a built-in autofocus that locks onto the background, a
bookshelf or a poster, and leaves you blurry. They almost always offer a
*manual* focus control, though. webcam-autofocus uses it: it finds your face,
measures how sharp it is, and moves the focus to the sharpest position, all in
the background and with little CPU.

## How you use it: a second, virtual camera

webcam-autofocus does not change your real camera, it sits between the camera and your
video-call program:

```
  real camera ──► webcam-autofocus ──► virtual camera "<your camera> (Autofocus)" ──► Zoom, Teams, browser, OBS …
  (blurry)         finds your face,      (the same picture, sharp on your face)
                   sets the focus
```

- After you switch autofocus on, your system has **one more camera** named
  **"&lt;your camera&gt; (Autofocus)"**. It is created automatically, you do not set anything up.
- **In your video-call program, choose that camera** instead of the real one. That is the whole
  trick: whatever program uses the virtual camera gets the sharp picture.
- Only one program can use a real camera at a time, and webcam-autofocus is that program.
  So the real camera cannot be selected in Zoom while autofocus is on, and that is intended.
- The virtual camera only shows a picture while autofocus is **on**. Switch it off and the
  camera goes dark; your real camera is handed back to its own autofocus.
- The window shows the exact name of the virtual camera (with a copy button), because
  v4l2loopback cuts names to 31 characters.

## Features

- **Autofocus on the face, not the scene:** contrast autofocus measured only on
  the detected face (OpenCV), with a coarse search followed by hill climbing.
- **Learns your setup:** the relation between face size and focus is learned
  and stored, so a refocus only searches near the predicted value.
- **Calm:** the focus is held when you turn your head away or look down, and it
  only refocuses when the picture has become blurrier than you allow (slider "Allowed blur",
  default 35 %), and the lens only moves if the new position is clearly sharper.
- **Virtual camera for video calls:** the corrected picture appears as
  *"&lt;your camera&gt; (Autofocus)"* (via v4l2loopback), so you know which one to
  choose in Zoom, Teams, Google Meet, OBS, Discord, a browser and so on.
- **Any camera with manual focus:** pick the camera in the window. The focus
  range is read from the camera, and every camera gets its own calibration.
- **Tray icon** (StatusNotifierItem: COSMIC, KDE, GNOME with the AppIndicator
  extension, ...): left click switches on/off, the lens is green (on), red (off)
  or orange (problem). A small window shows the focus slider, a sharpness graph,
  an optional preview and a log.
- **Manual focus when you want it:** tick *Manual focus* and move the slider yourself
  (drag or mouse wheel); green means sharp, orange means not sharp.
- **Light on the CPU:** only every n-th frame is analysed (adjustable, or
  automatic by CPU load).
- **Terminal view** (`webcam-autofocus`) for headless setups and debugging.

## Install

### Debian / Ubuntu / Pop!\_OS (.deb)

Download the `.deb` from the [releases page](https://github.com/grauschnabel/webcam-autofocus/releases), then:

```sh
sudo apt install ./webcam-autofocus_*_all.deb
```

This pulls in Python 3 with OpenCV, GTK 4 / libadwaita 1.4, `v4l-utils`, `ffmpeg` and
the `v4l2loopback` kernel module (built with DKMS). The virtual camera is created with
`v4l2loopback-ctl add`, which needs v4l2loopback 0.13 or newer: Debian 13, Ubuntu 25.04 or
newer and Pop!\_OS 24.04 have it, the stock package of Ubuntu 24.04 (0.12.7) does not.

> **Secure Boot:** the loopback module is built locally and must be signed.
> If `sudo modprobe v4l2loopback` fails, check `mokutil --sb-state` and enrol
> the DKMS signing key, or disable Secure Boot.

Start **Webcam Autofocus** from your application menu.

### From source

```sh
sudo apt install python3-opencv opencv-data python3-numpy python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 v4l-utils ffmpeg v4l2loopback-dkms v4l2loopback-utils
git clone https://github.com/grauschnabel/webcam-autofocus
cd webcam-autofocus
./setup.sh              # loads v4l2loopback (needs sudo)
./autofocus-tray --start
```

## Usage

1. Start **Webcam Autofocus** from your application menu. A lens icon appears in the
   panel (green = on, red = off, orange = problem), and the window opens.
2. Choose your **camera** at the top of the window. Cameras without a manual focus
   (many built-in laptop cameras are fixed-focus) are not offered; the window says which ones.
3. Switch autofocus **on**: the switch at the top left of the window, or a left click on the
   tray icon. Look into the camera for a moment; the first focusing takes a few seconds.
4. In your video-call program, select the camera shown in the highlighted row of the
   window, **"&lt;your camera&gt; (Autofocus)"**.
5. Leave the window or close it (the tray icon keeps running). Switch autofocus off when you
   do not need it.

**Manual focus.** Tick *Manual focus* above the slider to set the focus yourself: drag the
slider or turn the mouse wheel over it. The autofocus does not search meanwhile, it only tells
you: the slider turns **green** when the picture is sharp and **orange** when it is not. It can
judge that once you moved the slider over a little range (it compares with the sharpest picture
it has seen). Untick the box and the autofocus takes over again and focuses anew.

**About the administrator password.** The virtual camera is a device of the system, so creating
it needs administrator rights (and it is created again after every reboot). With the `.deb`
installed, local users get this without a password through a polkit rule that allows exactly
one small helper (`/usr/lib/webcam-autofocus/create-virtual-camera`). If your session is not
allowed to (a remote session, for example), or when you run from a source checkout, the app first
explains why it needs the rights and shows the equivalent terminal command, which you can copy
instead of using the password dialog.

### Command line

```sh
webcam-autofocus-tray [--start] [--hidden]   # tray app; --start switches on, --hidden shows no window
webcam-autofocus                             # terminal view
webcam-autofocus --help                      # all options
```

Useful options (all of them work for both commands):

| Option | Meaning |
|--------|---------|
| `--device /dev/videoN` | use this camera instead of the one selected in the window |
| `--fmin N`, `--fmax N` | limit the focus range that is searched (default 1–300, clamped to what the camera offers) |
| `--eval-every N` | analyse only every n-th frame (default 10) |
| `--blur F` | how much the sharpness may drop before refocusing, 0.15–0.6 (default 0.35; also a slider in the window) |
| `--freeze` | the virtual camera shows a still picture while the lens searches (also a checkbox in the window) |
| `--dynamic` | adapt `--eval-every` to the CPU load |
| `--no-output` | only control the focus, no virtual camera |
| `--reset` | ignore the stored calibration |

## Does my camera work?

It needs a manual focus control. Check with:

```sh
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-ctrls | grep focus
```

If you see `focus_absolute` (and usually `focus_automatic_continuous`), it should work.
Fixed-focus cameras have nothing to control. It has been developed and tested with a single
USB webcam so far; reports for other cameras are welcome, see [Contributing](CONTRIBUTING.md).

The default search range is 1–300. If your camera's sharp range lies further up
(some report values up to 1000 or more), raise `--fmax`.

## How it works

1. A Haar cascade finds your face; sharpness is the variance of the Laplacian inside
   the face box.
2. The first focus is searched coarsely over the range and then refined. Each result is
   stored as *face width → focus*.
3. From these points a line is fitted. Later searches only sweep around the predicted
   value, and only when the sharpness stayed more than the allowed blur below the value
   measured after the last focusing. The lens only moves if the result is clearly
   (15 %) sharper than the old position.
4. The camera's own continuous autofocus is switched off while the program runs and
   switched back on when you stop it.

## Troubleshooting

- **"No image from the camera":** another program (Zoom, a browser tab) is using the
  real camera. Select the virtual camera there instead.
- **Camera disappears for a moment** (USB errors in `dmesg`): switch autofocus off
  and on again. Cameras on USB hubs or docks are more prone to this.
- **The virtual camera is missing in Zoom:** restart the video-call program after
  the camera was created; check `v4l2-ctl --list-devices`.
- **"Could not create the virtual camera":** is the `v4l2loopback` module loaded
  (`lsmod | grep v4l2loopback`)? Is `pkexec` installed? As a workaround run
  `sudo v4l2loopback-ctl add -n "<name shown in the window>" -x 1` yourself.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE) © 2026 Martin Kaffanke
