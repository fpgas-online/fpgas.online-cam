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
#   CAM_SRC    GStreamer source element/bin (default: libcamerasrc)
#   RTMP_DEST  rtmp:// URL to publish to (default: rtmp://<gateway>/pib/<host>)
#   FPS        capture frame rate (default: 6)
#   GOP        keyframe interval in frames (default: FPS, i.e. 1 s)
#   WIDTH      capture width  (default: camera's choice; 640 on the
#   HEIGHT     capture height  software-encode path, see below)
#   RTSP_DEST  rtsp:// URL for the WebRTC leg (default: mediamtx on the
#              gateway, rtsp://<gateway>:8554/cam/<host>; empty disables)
FPS=${FPS:-6}
GOP=${GOP:-${FPS}}
CAM_SRC=${CAM_SRC:-libcamerasrc}

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

# Second leg: publish the same encoded stream to mediamtx over RTSP for
# WebRTC/WHEP delivery (fpgas.online-infra roles/cam/webrtc). RTMP relay
# between nginx-rtmp and mediamtx is broken in BOTH directions (gortmplib
# handshake/parse errors, tested 2026-09-02 against mediamtx v1.20.1), so
# the Pi tees the one encode into both servers itself.
if [ -z "${RTSP_DEST+x}" ]; then
    hn=${hn:-$(/usr/bin/hostname --short)}
    ip=${ip:-$(ip -json route show default | jq ".[0].gateway" --raw-output)}
    RTSP_DEST=rtsp://${ip}:8554/cam/${hn}
fi

# The whole pipeline dies (and systemd restarts it, taking the HLS feed
# down with it) if rtspclientsink cannot connect, so only add the RTSP
# branch when something is listening -- an undeployed or briefly down
# mediamtx must not flap the RTMP leg. If mediamtx comes back later the
# leg reappears on the next service restart.
RTSP_BRANCH=""
if [ -n "${RTSP_DEST}" ]; then
    hostport=${RTSP_DEST#rtsp://}
    hostport=${hostport%%/*}
    rtsp_host=${hostport%%:*}
    rtsp_port=${hostport##*:}
    if [ "${rtsp_port}" = "${hostport}" ]; then rtsp_port=554; fi
    if timeout 2 bash -c "true > /dev/tcp/${rtsp_host}/${rtsp_port}"; then
        RTSP_BRANCH="t. ! queue ! rtspclientsink location=${RTSP_DEST} protocols=tcp latency=0"
    else
        echo "RTSP probe ${rtsp_host}:${rtsp_port} failed; publishing RTMP only" >&2
    fi
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
    # encoder). Only when capturing from the real camera: a test CAM_SRC
    # (tests/ci) pins its own size and a second capsfilter would fail
    # caps negotiation rather than scale.
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

# ${CAM_SRC}, ${venc} and ${RTSP_BRANCH} are deliberately unquoted: they are
# pipeline fragments.
# shellcheck disable=SC2086
/usr/bin/gst-launch-1.0 ${CAM_SRC} ! \
    video/x-raw,${SIZE_CAPS}colorimetry=bt709,format=NV12,interlace-mode=progressive,framerate=${FPS}/1 ! \
    clockoverlay shaded-background=true !\
    ${venc} !\
    video/x-h264,profile=high,level=\(string\)4.2 ! \
    h264parse ! \
    tee name=t \
    t. ! queue ! flvmux ! \
    rtmpsink location="${RTMP_DEST}" \
    ${RTSP_BRANCH}
