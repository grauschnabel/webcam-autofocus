#!/usr/bin/env bash
# One-time setup: OpenCV, GTK 4 / libadwaita, ffmpeg, v4l2-ctl and v4l2loopback. The program creates the virtual
# cameras ("<camera> (Autofocus)") itself (via pkexec) the first time a camera is used.
set -e
sudo apt install -y python3-opencv opencv-data python3-numpy python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 \
  v4l-utils ffmpeg v4l2loopback-dkms v4l2loopback-utils
# The virtual cameras are created with `v4l2loopback-ctl add`, which exists from v4l2loopback 0.13 on
# (Ubuntu 24.04 ships 0.12.7).
version="$(dpkg-query -W -f='${Version}' v4l2loopback-utils)"
dpkg --compare-versions "$version" ge 0.13 || { echo "v4l2loopback $version is too old, 0.13 or newer is needed"; exit 1; }
echo 'options v4l2loopback devices=0 exclusive_caps=1' | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
sudo modprobe -r v4l2loopback 2>/dev/null || true
sudo modprobe v4l2loopback || { echo "modprobe failed — Secure Boot? (mokutil --sb-state)"; exit 1; }
echo "Done: v4l2loopback is loaded."
