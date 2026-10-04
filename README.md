# fpgas.online-cam

Camera capture and GStreamer streaming scripts for [fpgas.online](https://fpgas.online) Raspberry Pi boards.

## Overview

Provides the camera capture pipeline that streams live video from Raspberry Pi cameras to the fpgas.online server. Users can watch their FPGA designs running on real hardware through the web interface.

## Scripts

| File | Purpose |
|------|---------|
| `gst-libcam.sh` | GStreamer pipeline using libcamera for local RTMP streaming |
| `gst-libcam-yt.sh` | GStreamer pipeline for YouTube live streaming |
| `cam.sh` | Wrapper script for camera capture |
| `cam.service` | systemd service unit for automatic camera startup |
| `cam-lens.py` | Installed as `fpgas-cam-lens`: finds out whether the CSI camera has a lens motor and hands the lens to libcamera (below) |
| `af/ov5647.json` | The autofocus section added to libcamera's stock OV5647 tuning for modules with a lens motor |

## Lens motor (autofocus cameras)

Some boards carry an OV5647 module with a lens motor (the Acorn hosts at Welland), others the fixed-focus
module. Every Pi boots the same image and `config.txt`, and cameras get swapped, so the stream finds out for
itself at every start, on the Pi, and keeps no per-board focus setting anywhere:

1. `gst-libcam.sh` finds a CSI camera and runs `fpgas-cam-lens`.
2. `fpgas-cam-lens` looks in the device tree for a lens motor node beside the bound sensor. The firmware's
   `ov5647` overlay always describes one (`ad5398@c`), disabled. It then asks the chip itself: a short capture
   powers the camera (the chip is silent without it) and the lens address is read over I2C.
3. No answer: a fixed-focus camera. Nothing is changed and the stream starts as it always did.
4. An answer: the camera receiver and the sensor are unbound, a runtime device-tree overlay makes the two
   changes the overlay's `vcm` parameter would have made (lens node to `okay`, `lens-focus` on the sensor), and
   both are bound again. A lens sub-device with a focus control appears. The helper writes
   `/run/fpgas-cam/<sensor>_af.json` (the stock libcamera tuning plus `af/<sensor>.json`) and prints its path.
5. `gst-libcam.sh` then runs `libcamerasrc af-mode=continuous` with that tuning file. libcamera scans for the
   sharpest lens position from the picture when the stream starts, and again only if the picture goes soft.

Run on hardware with this code (5 Oct 2026, two Pi 5s, kernel 6.12.109+rpt-rpi-v8, libcamera
0.5.2+rpt20250903, files copied into the running system and `systemctl restart fpgas-cam`):

- Fixed-focus OV5647: the helper logged "no chip answers at 0x0c while the camera is powered: fixed-focus
  camera", changed nothing, and the stream started with the stock tuning as before.
- Autofocus OV5647 about 10 cm above an Acorn: the helper logged "a lens chip answers at 0x0c: binding its
  driver", unbound and bound receiver and sensor, and an `ad5398 focus` sub-device appeared; the stream started
  with `af-mode=continuous` and `/run/fpgas-cam/ov5647_af.json`, scanned, and came to rest at lens code 310
  within about 15 s; it then held that code for the 190 s it was watched. The picture is sharp (LEDs, part
  markings and a QR code readable). A second start in the same boot logged "lens driver already bound" and
  focused again.
- With libcamera's stock re-trigger values the lens re-scanned about every 80 s on that still scene and rested
  at a different code each time (302 to 374); `af/ov5647.json` therefore scans once at start and holds.
- A hand sweep of the same camera the evening before found code 384 sharpest and libcamera chooses about 310;
  both give a sharp picture. A second camera of the same kind is sharpest by hand at code 320, which is why
  one fixed position for the fleet cannot work.

Not yet run: a start in the dark (only the board's LEDs lit).

Two things that are deliberate:

- **The receiver is unbound together with the sensor.** Re-probing only the sensor while `rp1-cfe` stays bound
  makes the receiver register its video devices a second time; the kernel oopses in `cfe_async_complete` and
  the Pi then hangs in shutdown until it is power-cycled. Never do that.
- **A lens that answers but cannot be set up is an error** (`fpgas-cam-lens` exits 1 and says why); the stream
  still starts, unfocused, and says so in the journal. A fixed-focus camera is not an error.

Not exercised on hardware: Pi 3 and Pi 4 hosts (unicam receiver, VC4 tuning). The helper finds the receiver by
following the sensor's CSI-2 link in the device tree rather than by name, and picks the VC4 tuning there, but no
autofocus module has been tried on one.

## Packaging

Built as a `.deb` package via [nfpm](https://nfpm.goreleaser.com/) and hosted in the [fpgas.online apt repo](https://github.com/fpgas-online/apt).

The deb installs:
- Scripts to `/usr/local/bin/`
- systemd service to `/usr/lib/systemd/system/`

## Installation

After adding the fpgas.online apt repository:

```bash
apt install fpgas-online-cam
systemctl enable --now fpgas-cam.service
```

This is normally handled by the [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) Ansible `cam/pi` role.

## Dependencies

- `gstreamer1.0-tools`
- `gstreamer1.0-plugins-base`
- `gstreamer1.0-plugins-good`
- `libcamera-tools`
- `python3`, `device-tree-compiler`, `kmod` (for `fpgas-cam-lens`)

## Linting

- **shellcheck**: blocking

## Related Repos

- [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) -- Ansible deployment (cam/pi and cam/stream-server roles)
- [apt](https://github.com/fpgas-online/apt) -- APT package repository hosting the deb

## License

Apache 2.0
