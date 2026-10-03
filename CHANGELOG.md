# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Every focus search writes one line (reason, start and end focus, all measured points, duration,
  turns) to `~/.cache/webcam-autofocus/search.log`, to find out why a focus went wrong.

## [0.1.0] - 2026-10-03

### Changed
- The first sweep stops only after a real peak (four times as sharp as the weakest point), not
  after a bump in the blur, so a cold start no longer ends at a wrong focus.
- The local search is one calm sweep upwards (no turning back while measuring) with a parabola
  through the best point, and the play of the lens is compensated in software: a move downwards
  is commanded lower, so every value means "arrived from below" and saved calibration stays valid.
  The reference sharpness is the median of five frames, like the running value, so a freshly found
  focus is no longer "re-checked" without reason.
- The focus search is systematic: a step that makes the picture sharper is followed by the next
  one in the same direction, a step that does not turns around, and the step size is halved once
  both sides have been tried. The regular check no longer moves the lens while the picture is
  still sharp.
- The lens is given time to arrive after every move (about 1 s per 60 units), so the search no longer
  measures the blur of the moving lens and no longer jumps up and down. The final approach always
  comes from below because the lens has play, the peak is interpolated with a parabola, and the
  first sweep uses finer steps and stops once it is past the peak.
- Before a password dialog for creating the virtual camera, the tray app explains why
  administrator rights are needed and offers the equivalent terminal command to copy. If polkit
  already allows it, the camera is created without asking.

- The name of the virtual camera is a highlighted row in the window with a copy button, and the
  README explains the virtual camera and how to use it in a video-call program.

### Fixed
- The switch in the window jumped back to "on" when switching off, because the engine needs a
  moment to stop; the switch and the tray icon now follow what was asked for.
- The window lists cameras without manual focus that are not offered, and the legend of the
  sharpness graph no longer depends on a font having a "■" glyph.
- Names of virtual cameras are cut to the 31 characters v4l2loopback keeps. A longer camera
  name (for example "C922 Pro Stream Webcam") was never found again, so autofocus could not
  be switched on and every attempt created another virtual camera.
- The helper that creates the virtual camera as root refuses names with a line break in
  them; its character check ran line by line and let them through.
- A missing `ffmpeg` is reported with a message instead of a traceback, and a failed start
  releases the camera again. `setup.sh` now also installs `ffmpeg`, GTK 4 and libadwaita.
- The `.deb` requires libadwaita 1.4 and v4l2loopback 0.13 or newer. Older libadwaita
  (Debian 12, Ubuntu 22.04) has no `Adw.ToolbarView`, so the window could not be opened, and
  the 0.12.7 of Ubuntu 24.04 has no `v4l2loopback-ctl add`, so the virtual camera could never
  be created there. The README names the releases that work, and `setup.sh` stops with a
  message when it finds an older v4l2loopback.
- A search without a detected face no longer stores a calibration point, and the
  calibration of a camera that is plugged in after the program started is loaded instead of
  being overwritten.
- Switching autofocus off and on again starts from a clean state: the three seconds of
  waiting for a face apply again, and the old sharpness reference no longer makes the
  window say "waiting for face" while it is tracking.
- Cameras without a `focus_automatic_continuous` control work. A failing or hanging
  `v4l2-ctl` is reported with its reason instead of blocking the engine.
- A calibration that only contains unsharp measurements no longer ends the program
  without a message (NumPy aborted inside LAPACK).
- A failed focus probe is repeated after 30 seconds instead of hiding the camera until the
  next start, and camera names that the kernel cut in the middle of a character are read.
- The tray icon answers property requests of the panel quickly (the icon image is built once
  per look instead of for every request), and an exception in the window update is printed
  once instead of silently stopping the updates.
- SIGTERM and SIGHUP (logout, `kill`, closing the terminal) hand the camera back to its own
  autofocus in the tray app and the terminal view.
- The "show face frame" setting of the preview survives switching the camera.

### Added
- Face-tracking contrast autofocus: sharpness is measured on the detected face, the
  camera's manual focus is driven through V4L2, and a learned face-size-to-focus model
  avoids searching on every movement.
- Virtual camera "<camera name> (Autofocus)" (v4l2loopback + ffmpeg) for Zoom, Teams,
  Google Meet, OBS and similar programs. It is created on demand with `pkexec`.
- Camera selection in the window; works with any camera that has a manual focus control.
  The focus range and search steps are adapted to the camera, and the learned calibration
  is stored per camera.
- Tray icon (StatusNotifierItem) with a control window: focus slider, sharpness graph,
  optional preview, adjustable evaluation rate (optionally by CPU load).
- Terminal view (`webcam-autofocus`).
- The focus is held while the face is briefly out of view (head turned).
- Debian package.
