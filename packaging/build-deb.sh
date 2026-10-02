#!/bin/sh
# Builds dist/webcam-autofocus_<version>_all.deb from the current source tree.
#
# Usage: packaging/build-deb.sh [version]
#
# Without an argument the version comes from webcam_autofocus/__init__.py. A pre-release
# suffix such as "-rc1" becomes "~rc1" so that it sorts before the final release.
set -eu

# Make file modes independent of the caller's umask.
umask 022

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NAME=webcam-autofocus
APP_ID=io.github.WebcamAutofocus

if [ $# -ge 1 ]; then
    VERSION="$1"
else
    VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$ROOT/webcam_autofocus/__init__.py")"
fi
VERSION="$(printf '%s' "$VERSION" | sed 's/-\(rc\|alpha\|beta\)/~\1/')"

PKG="$ROOT/dist/${NAME}_${VERSION}_all"
LIB="$PKG/usr/lib/python3/dist-packages/webcam_autofocus"
DOC="$PKG/usr/share/doc/$NAME"

rm -rf "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/usr/bin" "$LIB" "$DOC" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/etc/modules-load.d" "$PKG/etc/modprobe.d" \
         "$PKG/usr/lib/$NAME" "$PKG/usr/share/polkit-1/actions"

install -m644 "$ROOT"/webcam_autofocus/*.py "$LIB/"
# Version of the package and of the program must match, also for release candidates.
sed -i "s/^__version__ = .*/__version__ = \"$VERSION\"/" "$LIB/__init__.py"

# Launchers: one for the tray app, one for the terminal view.
for spec in "$NAME-tray:tray" "$NAME:cli"; do
    cat > "$PKG/usr/bin/${spec%%:*}" <<LAUNCHER
#!/usr/bin/python3
from webcam_autofocus.${spec##*:} import main

main()
LAUNCHER
    chmod 755 "$PKG/usr/bin/${spec%%:*}"
done

install -m644 "$ROOT/packaging/$APP_ID.desktop" "$PKG/usr/share/applications/$APP_ID.desktop"
install -m644 "$ROOT/packaging/$NAME.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/$NAME.svg"
# Root helper plus polkit action: creating a virtual camera works without a password for the local user.
install -m755 "$ROOT/packaging/create-virtual-camera" "$PKG/usr/lib/$NAME/create-virtual-camera"
install -m644 "$ROOT/packaging/io.github.webcamautofocus.policy" \
    "$PKG/usr/share/polkit-1/actions/io.github.webcamautofocus.policy"
install -m644 "$ROOT/packaging/copyright" "$DOC/copyright"
install -m644 "$ROOT/CHANGELOG.md" "$DOC/changelog"
gzip -9n "$DOC/changelog"
install -m644 "$ROOT/README.md" "$DOC/README.md"

# Load v4l2loopback at boot without pre-created devices; the program adds one per camera.
echo "v4l2loopback" > "$PKG/etc/modules-load.d/$NAME.conf"
echo "options v4l2loopback devices=0 exclusive_caps=1" > "$PKG/etc/modprobe.d/$NAME.conf"
chmod 644 "$PKG/etc/modules-load.d/$NAME.conf" "$PKG/etc/modprobe.d/$NAME.conf"

install -m755 "$ROOT/packaging/postinst" "$PKG/DEBIAN/postinst"
printf '/etc/modules-load.d/%s.conf\n/etc/modprobe.d/%s.conf\n' "$NAME" "$NAME" > "$PKG/DEBIAN/conffiles"

SIZE="$(du -sk "$PKG/usr" | cut -f1)"

cat > "$PKG/DEBIAN/control" <<CONTROL
Package: $NAME
Version: $VERSION
Section: video
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-opencv, python3-numpy, python3-gi, python3-gi-cairo, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.4), opencv-data, v4l-utils, ffmpeg, v4l2loopback-dkms (>= 0.13), v4l2loopback-utils (>= 0.13), pkexec | policykit-1
Maintainer: Martin Kaffanke <martin@kaffanke.info>
Homepage: https://github.com/grauschnabel/webcam-autofocus
Description: Face-tracking autofocus for webcams, with a virtual camera for video calls
 Keeps a Linux webcam sharp on your face instead of the background. It measures
 sharpness on the detected face, drives the camera's manual focus (V4L2) and
 sends the result to a virtual camera that you select in Zoom, Teams, Google
 Meet, OBS and similar programs. Comes with a tray icon, a small control window
 and a terminal view. Works with any camera that offers a manual focus control.
CONTROL

fakeroot dpkg-deb --root-owner-group --build "$PKG" "$ROOT/dist/" >/dev/null
echo "$ROOT/dist/${NAME}_${VERSION}_all.deb"
