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

## Linting

- **shellcheck**: blocking

## Related Repos

- [fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) -- Ansible deployment (cam/pi and cam/stream-server roles)
- [apt](https://github.com/fpgas-online/apt) -- APT package repository hosting the deb

## License

Apache 2.0
