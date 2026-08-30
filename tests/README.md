# Latency tests

The camera path is `libcamerasrc -> H.264 -> RTMP -> nginx-rtmp (HLS) ->
video.js`. Its latency is set by the HLS fragment length (nginx-rtmp only cuts
fragments at keyframes, so the GOP is the floor) and by video.js, which starts
3 x `TARGETDURATION` behind the newest listed fragment. Every frame carries the
Pi's wall clock (`clockoverlay`), which is what these tests read back.

## In CI (no hardware)

`.github/workflows/e2e-latency.yml` builds `tests/ci/Dockerfile` and runs
`tests/ci/e2e.py`, which starts an unprivileged nginx-rtmp (`tests/ci/nginx.conf`
mirrors the infra repo's `pib.conf.j2` / `live-hls.conf.j2` -- keep them in
step), runs the real `gst-libcam.sh` with `CAM_SRC=videotestsrc` and
`RTMP_DEST=rtmp://127.0.0.1:11935/pib/test`, opens `tests/ci/player.html`
(the site's video.js 8.4.0) in Debian's chromium and fails if the median
measured latency exceeds `--max-latency` (8 s; ~5 s expected).

CI exercises the `x264enc` branch of the script; the Pi 4's `v4l2h264enc`
branch (keyframe interval via `h264_i_frame_period`) can only be checked on
hardware.

Locally, with docker:

    docker build -t fpgas-cam-e2e -f tests/ci/Dockerfile tests/ci
    docker run --rm -v "$PWD:/src:ro" fpgas-cam-e2e uv run --no-project tests/ci/e2e.py

(Add `--network host` to both if containers on your machine have no DNS, as
on ten64.) Everything binds 127.0.0.1:18080 (http) and :11935 (rtmp).

Playwright's own Chromium has no H.264 and cannot play this stream at all
(video.js: `MEDIA_ERR_SRC_NOT_SUPPORTED`); the image uses Debian's chromium.

## On real hardware

Point the same measurement tool at a live board page from any machine with
an H.264-capable Chromium and tesseract (or from the docker image):

    node tests/measure-latency.mjs https://tinytapeout.fpgas.online/board/tt04/ \
        --samples 5 --source-tz Europe/London

`--source-tz` is the zone of the clock in the picture, i.e. the Pi's local
zone (`date +%Z` on the Pi); the Pis and the viewer must both be NTP-synced.
Besides the OCR'd glass-to-glass figure it prints
`newest_listed_fragment_age_s`, the structural part video.js can see: the
age of the newest fragment in the playlist (`hls_fragment_naming system`
puts the open time in the name). The difference between the two is the
camera->server leg (well under 1 s on both Pi 4 and Pi 5, measured
2026-08-30).

To check the Pi -> server leg alone, decode the first frame of the newest
segment on a Pi and compare its clock with the segment name:

    curl --resolve tinytapeout.fpgas.online:443:10.21.0.1 -O https://tinytapeout.fpgas.online/live/<newest>.ts
    gst-launch-1.0 filesrc location=<newest>.ts ! tsdemux ! h264parse ! openh264dec \
        ! videoconvert ! video/x-raw,format=RGB ! pngenc snapshot=true ! filesink location=frame0.png
