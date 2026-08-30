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

# figure out if we can use v4l2 hardware encoding (pi 5 says No.)
if (gst-inspect-1.0 --exists v4l2h264enc); then
    # h264_i_frame_period: the bcm2835 codec defaults to 60 frames.
    venc="v4l2h264enc extra-controls=controls,video_bitrate_mode=0,video_bitrate=1000000,repeat_sequence_header=1,h264_i_frame_period=${GOP}"
else
    venc="x264enc bitrate=2000 byte-stream=false key-int-max=${GOP} bframes=0 aud=true tune=zerolatency"
fi

# example of using encode bin to select encoder
# gst-launch-1.0 videotestsrc ! video/x-raw,width=640,height=480 ! queue ! \
#   encodebin ! h264parse ! qtmux ! filesink location=output.mp4

# ${CAM_SRC} and ${venc} are deliberately unquoted: they are pipeline fragments.
# shellcheck disable=SC2086
/usr/bin/gst-launch-1.0 ${CAM_SRC} ! \
    video/x-raw,colorimetry=bt709,format=NV12,interlace-mode=progressive,framerate=${FPS}/1 ! \
    clockoverlay shaded-background=true !\
    ${venc} !\
    video/x-h264,profile=high,level=\(string\)4.2 ! \
    h264parse ! \
    queue ! flvmux ! \
    rtmpsink location="${RTMP_DEST}"
