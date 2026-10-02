#!/usr/bin/env bash
# Einmalige Einrichtung: OpenCV + virtuelle Kamera (v4l2loopback) als /dev/video10
set -e
sudo apt install -y python3-opencv opencv-data v4l2loopback-dkms v4l2loopback-utils
echo 'options v4l2loopback devices=1 video_nr=10 card_label="Dell Webcam (Autofokus)" exclusive_caps=1' \
  | sudo tee /etc/modprobe.d/v4l2loopback.conf >/dev/null
echo v4l2loopback | sudo tee /etc/modules-load.d/v4l2loopback.conf >/dev/null
sudo modprobe v4l2loopback || { echo "modprobe fehlgeschlagen — Secure Boot? (mokutil --sb-state)"; exit 1; }
ls -l /dev/video10 && echo "Fertig: virtuelle Kamera /dev/video10 ist da."
