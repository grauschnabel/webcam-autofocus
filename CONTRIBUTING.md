# Contributing

Bug reports and small, focused pull requests are welcome. Reports for cameras that
do or do not work are especially useful.

## Setup

```sh
sudo apt install python3-opencv opencv-data python3-numpy python3-gi python3-gi-cairo \
  gir1.2-gtk-4.0 gir1.2-adw-1 v4l-utils ffmpeg v4l2loopback-dkms v4l2loopback-utils \
  fakeroot python3-pyflakes
./autofocus-tray        # run the tray app from the checkout
python3 -m unittest     # run the tests
make deb                # build dist/webcam-autofocus_<version>_all.deb
```

## Before opening a pull request

- `python3 -m pyflakes .` must print nothing and `python3 -m unittest` must pass (CI runs the same).
- Add a test for logic changes (`tests/`) and an entry in `CHANGELOG.md`.
- Keep the project small: no cloud services, no telemetry, no settings that need a database.
