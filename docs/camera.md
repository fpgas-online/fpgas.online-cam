# The camera on a Pi host

**You operate the fleet and want to know how a Pi's camera reaches the board's page, to fix or change a
feed.** From this repository's [`gst-libcam.sh`](../gst-libcam.sh) and [`cam.service`](../cam.service) (main
0c974d5) and the gateway roles `stream_server` and `webrtc` in
[fpgas.online-infra](https://github.com/fpgas-online/fpgas.online-infra) (main, read 2026-10-07).

## On the Pi

`fpgas-cam.service` runs `/usr/local/bin/fpgas-gst-libcam.sh`, which:

1. finds a camera: a CSI camera through libcamera first, else a USB (UVC) capture device, such as the HDMI
   grabbers on the NeTV2 hosts;
2. if it finds neither after a few tries, exits with status 78. The unit does not restart on 78
   (`RestartPreventExitStatus=78`): a CSI camera can only be connected with the board off. After plugging in a
   USB grabber: `sudo systemctl restart fpgas-cam`;
3. encodes H.264, with the hardware encoder where there is one and `x264enc` where there is not (a Pi 5),
   with one keyframe per second, and a clock drawn over the picture;
4. publishes to `rtmp://<the Pi's default gateway>/pib/<the Pi's short name>`.

Any other failure (the stream server restarting, say) is retried every second.

## On the gateway

`stream_server` runs nginx-rtmp, which takes the Pi's stream and serves it as HLS under `/live`
(`https://<site>/live/<name>.m3u8`, which a desktop player such as VLC opens). Where `webrtc_additional_hosts`
is set (in `host_vars/fpgas.online.yml`, so on tweed), `webrtc` runs mediamtx, which serves the same streams
over WebRTC (WHEP).

On the gateway, `systemctl status nginx mediamtx` and `journalctl -u nginx` show whether a stream arrives; on
the Pi, `journalctl -u fpgas-cam` shows the pipeline's own messages.

## Which Pis have a camera

No list is kept per Pi. On a Pi, `systemctl status fpgas-cam` says: running, or stopped with status 78 where
there is no camera.
