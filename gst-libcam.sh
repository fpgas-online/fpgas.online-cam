#!/bin/bash -x

# Camera -> H.264 -> RTMP publisher for fpgas.online Pis (fpgas-cam.service).
#
# Latency budget: nginx-rtmp can only cut an HLS fragment at a keyframe, so
# the GOP length is the floor on fragment length, and video.js (VHS) starts
# playback 3 x TARGETDURATION behind the newest listed fragment. With the old
# 60-frame GOP at 6 fps that was 10 s fragments and ~40 s glass-to-glass
# (measured 2026-08-30). One keyframe per second + hls_fragment 900ms on the
# server (fpgas.online-infra roles/cam/stream-server) brings it to ~5 s.
#
# Overridable from the environment so tests/ci can run this exact script
# against a synthetic source and a local nginx-rtmp:
#   CAM_SRC    GStreamer source element/bin (default: detected, see below)
#   RTMP_DEST  rtmp:// URL to publish to (default: rtmp://<gateway>/pib/<host>)
#   FPS        capture frame rate (default: 6)
#   GOP        keyframe interval in frames (default: FPS, i.e. 1 s)
#   WIDTH      capture width   (default: the source's own choice; 640x540
#   HEIGHT     capture height   on the CSI software-encode path, see below)
FPS=${FPS:-6}
GOP=${GOP:-${FPS}}
GST_LAUNCH=${GST_LAUNCH:-/usr/bin/gst-launch-1.0}

# Capture source, when CAM_SRC is not given:
#   1. a CSI camera: the firmware found a sensor at boot and the kernel bound
#      it, e.g. v4l-subdev "ov5647 10-0036" (<driver> <i2c bus>-<addr>), on
#      Pi 3/4 (unicam) and Pi 5 (rp1-cfe) alike;
#   2. else a USB (UVC) capture device, e.g. the HDMI grabbers on the NeTV2
#      boards, as raw YUY2 1280x720 (10 fps on the MS2109): no MJPEG decode;
#   3. else no camera. CSI cameras can only be connected with the board off,
#      and USB ones are enumerated long before this runs, so after
#      CAM_WAIT_TRIES checks exit 78, which cam.service does not restart:
#      a board without a camera stops here instead of looping for ever.
V4L_SYSFS=${V4L_SYSFS:-/sys/class/video4linux}
V4L_BYID=${V4L_BYID:-/dev/v4l/by-id}
CAM_WAIT_TRIES=${CAM_WAIT_TRIES:-10}
CAM_WAIT_SECS=${CAM_WAIT_SECS:-3}

find_camera() {
    if grep -qsE ' [0-9]+-00[0-9a-f]{2}$' "${V4L_SYSFS}"/v4l-subdev*/name; then
        CAM_SRC=libcamerasrc
        return 0
    fi
    for dev in "${V4L_BYID}"/usb-*-video-index0; do
        [ -e "${dev}" ] || continue
        CAM_SRC="v4l2src device=${dev} ! video/x-raw,format=YUY2,width=1280,height=720 ! videorate ! videoconvert"
        return 0
    done
    return 1
}

if [ -z "${CAM_SRC:-}" ]; then
    try=1
    until find_camera; do
        echo "no camera found (try ${try}/${CAM_WAIT_TRIES})" >&2
        if [ "${try}" -ge "${CAM_WAIT_TRIES}" ]; then
            echo "no CSI sensor or USB capture device: giving up (exit 78, not restarted)." >&2
            echo "A CSI camera needs a power cycle; after plugging in USB: systemctl restart fpgas-cam" >&2
            exit 78
        fi
        try=$((try + 1))
        sleep "${CAM_WAIT_SECS}"
    done
fi

if [ -z "${RTMP_DEST:-}" ]; then
    # hostname
    hn=$(/usr/bin/hostname --short)

    # find the upstream IP and nic dev
    # this is clever, but should probably be an os var managed by ansible.
    ip=$(ip -json route show default | jq ".[0].gateway" --raw-output)

    # while [ "${hn}" = "localhost" ]
    # do
    #     echo ${hn} "is still localhost"
    #     # this should really be in its own systemd script.
    #     # it works around some bug in
    #     # https://github.com/isc-projects/dhcp/blob/master/client/scripts/linux#L121
    #     /usr/sbin/dhclient -v ${dev}
    #     hn=$(/usr/bin/hostname --short)
    # done

    RTMP_DEST=rtmp://${ip}/pib/${hn}
fi

# figure out if we can use v4l2 hardware encoding (pi 5 says No.)
if (gst-inspect-1.0 --exists v4l2h264enc); then
    # h264_i_frame_period: the bcm2835 codec defaults to 60 frames.
    venc="v4l2h264enc extra-controls=controls,video_bitrate_mode=0,video_bitrate=1000000,repeat_sequence_header=1,h264_i_frame_period=${GOP}"
else
    # speed-preset: gst's default is "medium", measured at ~1.3 cores for
    # 6 fps of 1280x1080 on a Pi 5 (which has no hardware H.264 encoder).
    # superfast is several times cheaper; the bitrate cost is irrelevant
    # for our static scenes (blinking LEDs).
    venc="x264enc bitrate=2000 byte-stream=false key-int-max=${GOP} bframes=0 aud=true tune=zerolatency speed-preset=superfast"
    # x264 cost scales with pixel count. Half of each dimension of the
    # 1280x1080 the camera negotiates by default is a ~4x saving with the
    # same field of view and aspect (the libcamera ISP scales before the
    # encoder). CSI only: every other source pins its own size upstream and
    # a second, different capsfilter would fail caps negotiation rather than
    # scale - the USB grabber leg built by find_camera() pins YUY2 1280x720
    # (the MS2109 offers nothing else at 10 fps), and a test CAM_SRC
    # (tests/ci) pins whatever the test asked for.
    if [ "${CAM_SRC}" = "libcamerasrc" ]; then
        WIDTH=${WIDTH:-640}
        HEIGHT=${HEIGHT:-540}
    fi
fi

# Optional size constraint (see WIDTH/HEIGHT above).
SIZE_CAPS=""
if [ -n "${WIDTH:-}" ]; then
    SIZE_CAPS="width=${WIDTH},height=${HEIGHT},"
fi

# example of using encode bin to select encoder
# gst-launch-1.0 videotestsrc ! video/x-raw,width=640,height=480 ! queue ! \
#   encodebin ! h264parse ! qtmux ! filesink location=output.mp4

# clockoverlay shading subtracts shading-value from the luma under the clock,
# so the default 80 leaves a light box on bright scenes (white bench, magenta
# PCB) and OCR fails; 200 keeps the white digits at >= 11.9:1 even on white (#9).
# ${CAM_SRC} and ${venc} are deliberately unquoted: they are pipeline fragments.
# shellcheck disable=SC2086
${GST_LAUNCH} ${CAM_SRC} ! \
    video/x-raw,${SIZE_CAPS}colorimetry=bt709,format=NV12,interlace-mode=progressive,framerate=${FPS}/1 ! \
    clockoverlay shaded-background=true shading-value=200 !\
    ${venc} !\
    video/x-h264,profile=high,level=\(string\)4.2 ! \
    h264parse ! \
    queue ! flvmux ! \
    rtmpsink location="${RTMP_DEST}"
