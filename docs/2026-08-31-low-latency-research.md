# Research: 2–3 frames of camera→browser latency (with 60 s rewind)

Date: 2026-08-31. Status: research / decision document — nothing here is
implemented yet. Companion to the measured work in `tests/README.md`.

## 1. Where we are and what "2–3 frames" means

Measured with `tests/measure-latency.mjs` (clockoverlay OCR, real Chromium):

| State | Glass-to-glass |
|---|---|
| Before 2026-08-30 fixes (60-frame GOP, 10 s HLS fragments) | **41.9 s** |
| After PR #4 + infra #33 (1 s GOP, 900 ms fragments) | **4.0–5.2 s** (CI harness; live redeploy pending) |
| Camera → server leg (Pi 4 hw enc and Pi 5 x264 alike) | **< 1 s** |

At the current 6 fps, "2–3 frames" is 330–500 ms; at 30 fps it is 66–100 ms.
Every "one frame of latency" element in the pipeline costs 167 ms at 6 fps but
33 ms at 30 fps, and auto-exposure may stretch shutter time toward the full
167 ms frame interval in dim light — so **raising capture to 30 fps is part of
any answer**, independent of transport (ov5647 does 1296x972 binned at up to
~43 fps, full field of view; the current 1280x1080 crop mode reads out in
~33 ms regardless of fps).

The dominant cost, however, is the delivery protocol. Segment-based HLS with
video.js has a hard floor of ≈4.5 × fragment length (video.js holds back
3 × TARGETDURATION, plus the fragment being written, plus playlist polling).
No codec or GOP change moves that floor below seconds. B-frames go the wrong
way entirely (they add reorder delay; we already run `bframes=0`).

## 2. The options

Researched 2026-08-31 (four parallel deep dives; full citations in the
session's reports, condensed below).

### 2.1 Tuned HLS (today's stack) — floor ~4.5 s

What we have after the August PRs. Keeps nginx-rtmp, video.js, trivially
CDN-able, iPhone-native. Kept regardless as the DVR/rewind track (60 s window,
infra PR #36 + site PR #15). Cannot go materially lower.

### 2.2 LL-HLS — 2–3 s real-world, ~1 s lab floor. **Not sufficient.**

- Spec mandates `PART-HOLD-BACK ≥ 3 × part duration`; with Apple's minimum
  sensible 200 ms parts that is ≥600 ms before encode/network/decode.
  Published aggressive experiments (spec-violating hold-back) reach 0.9–1.4 s;
  production consensus is 2–3 s.
- nginx-rtmp cannot produce it; mediamtx produces it by default.
- video.js's engine (VHS) still has **no LL-HLS support** (open since 2020);
  hls.js would be required.

### 2.3 HTTP-FLV / fMP4-over-WebSocket (MSE) — 0.5–1.5 s WAN. **Not sufficient, but cheap.**

- `nginx-http-flv-module` is a maintained, config-compatible superset fork of
  nginx-rtmp (v1.2.14, Jul 2026): drop-in server swap, played by mpegts.js
  (flv.js's maintained successor, with `liveSync` playbackRate catch-up).
- Below ~0.5 s of MSE buffer, TCP head-of-line blocking causes stalls on real
  networks; no credible evidence of sustained sub-300 ms MSE over WAN.
  LAN demos (raw NALs over WS + jMuxer) do reach 0.1–0.3 s.
- iPhone support via ManagedMediaSource is unconfirmed for mpegts.js.

### 2.4 MJPEG (multipart/x-mixed-replace) — hits the number, fails everything else

~0.2–0.35 s and dead simple, but: 4–12 Mbit/s **per viewer** at 1280×1080@6fps
(4–10× H.264 for equal quality), no rewind, no player UX, and our sources are
already H.264 — serving MJPEG would mean continuously transcoding all 19
streams server-side. Rejected except possibly as a single-camera "peek" mode.

### 2.5 WebCodecs + WebSocket/WebTransport — 0.2–0.5 s plausible. **Fallback candidate.**

The only non-WebRTC path with no protocol-mandated hold-back: browser
`VideoDecoder` (H.264 hardware decode) + app-chosen jitter buffer + canvas.
Support: Chrome 94+, Firefox desktop 130+, Safari 16.4+ (video-only decode
suffices for us; full in Safari 26); missing on Firefox Android. Costs: a
custom RTMP→NALs-over-WS fan-out shim, a canvas-based player (no native
controls/fullscreen-video/PiP), tab-throttling and reconnect handling, and
WebTransport for loss-resilience at the low end. Keep in reserve if WebRTC's
port requirements ever become a blocker; also where Media-over-QUIC is headed.

### 2.6 WebRTC (WHEP) — **200–400 ms measured. The recommendation.**

- Best independent benchmark found is exactly our shape: RTSP → mediamtx →
  browser on LAN, **P50 310 ms / P95 480 ms glass-to-glass** (mediamtx's own
  internal contribution 30–80 ms). H.264 passes through untranscoded; H.264
  recvonly is Safari's native hardware path, so iPhone works.
- Server comparison (RTMP/RTSP in, WHEP + HLS out, packaging, ports):
  - **mediamtx v1.20 — recommended.** The only candidate with standard WHEP
    + RTSP/RTMP push ingest + LL-HLS + record-to-disk with a
    timestamp-addressed fMP4 playback API. Single muxed UDP port (:8189, all
    sessions) + optional TCP fallback; `webrtcAdditionalHosts` for our public
    IP; no TURN needed. Static amd64/arm64 binaries → wraps into our nfpm/apt
    rolling-deb pipeline. Monthly releases. Cons: bus-factor 1; cannot ask
    publishers for keyframes (join waits ≤1 GOP = ≤1 s — true of every
    candidate; the Pi 4 encoder has no force-keyframe control anyway).
  - SRS v6: runner-up; zero Pi-side changes but ~500 ms on the RTMP path,
    Docker-first, WHEP unauthenticated by default.
  - go2rtc: good WebRTC leg, but HLS/DVR is a stub — fails the rewind
    requirement on its own.
  - Janus: no RTMP/HLS, port-range ICE, separate WHEP shim — too many parts.
  - Galene (no WHEP out), OvenMediaEngine (no standard WHEP, AGPL,
    Docker-only): rejected.

## 3. The welland network constraint (verified locally)

- There is **one public IPv4** (ten64.welland `87.121.95.37`); HTTPS :443 is
  SNI-routed by an nginx `stream{}` proxy **without decryption** (TCP-only,
  client-IP preserving). WebRTC's UDP media cannot ride it.
- IPv6 is direct: `tinytapeout.fpgas.online` AAAA → tweed itself.
- Therefore WebRTC needs: (a) WHEP signalling proxied like any HTTPS (fine —
  mediamtx documents reverse-proxying WHEP; must pass PATCH/DELETE), and
  (b) **one media port**, e.g. UDP+TCP 8189: an nftables `input accept` on
  tweed (IPv6 path) plus one DNAT rule on ten64 (IPv4 path), with
  `webrtcAdditionalHosts: [87.121.95.37, <tweed v6>]`. No TURN server.

## 4. Recommended architecture

```
Pi (30 fps, 1 s GOP, superfast/hw enc) ──RTSP (later WHIP)──▶ mediamtx on tweed
                                                                ├─ WHEP  ─▶ live view   (~150–400 ms)
                                                                ├─ HLS   ─▶ 60 s DVR / rewind + thumbnails
                                                                └─ (record+playback API — optional richer DVR)
```

- **Board page**: bare `<video muted autoplay playsinline>` driven by
  mediamtx's zero-dependency `reader.js` (ships with the server, infinite 2 s
  auto-reconnect) for live; the existing video.js+HLS player for rewind. A
  ~150-line controller swaps between them (seek → HLS at offset; "LIVE" →
  back to WHEP). video.js itself must NOT front the MediaStream (its
  srcObject handling is broken; no maintained generic WHEP plugin exists).
- **Thumbnail grid stays HLS**: no latency need, cacheable, avoids ~9 always-on
  RTP sessions per index visitor and Chrome's ungraceful many-PeerConnection
  failure modes.
- **Pi pipeline changes** (each independently testable with the existing
  harness): 30 fps capture; `speed-preset=superfast` on the Pi 5 x264 branch
  (gst default is `medium` — measured 1.3 cores at just 6 fps, and Pi 5 has
  no hardware encoder, confirmed); `queue max-size-buffers=1 leaky=downstream`
  + `sync=false`; keep 1 s GOP (`key-int-max=$FPS` already does this);
  repeat SPS/PPS before each IDR; verify h264parse is AU-aligned (else it
  silently adds one full frame). Ingest: keep RTMP during transition (mediamtx
  accepts it), then switch to `rtspclientsink latency=0` (packaged in Debian;
  WHIP would need gst-plugins-rs, which Debian does not package at all).

### Latency budget after all phases (30 fps)

| Stage | Budget |
|---|---|
| Exposure (capped by 33 ms interval) + readout | ~20–35 ms |
| ISP + encode (hw ≈10 ms / x264 superfast ≈15–30 ms) | ~15–30 ms |
| Pi → tweed RTP + mediamtx internal | ~10–80 ms |
| WebRTC jitter buffer + decode + render | ~50–200 ms (tunable via `jitterBufferTarget`) |
| **Total steady-state** | **~100–350 ms ≈ 3–10 frames @30 fps** |
| Join wait (once, next IDR) | ≤1 s |

At 6 fps the same stack lands ~300–500 ms (2–3 frames) — i.e. the target is
met at either frame rate; 30 fps just makes it feel much better.

## 5. Testing

- **CI (no hardware)**: extend the existing `tests/ci/` harness — add mediamtx
  (deb/binary in the docker image) beside nginx-rtmp, run the same
  `gst-libcam.sh` against it, and add a WHEP page; the clockoverlay-OCR
  measurement carries over unchanged (ground truth), with
  `inbound-rtp.jitterBufferDelay` / `estimatedPlayoutTimestamp` from
  `getStats()` as the structural metrics. Assert median < 0.5 s on the WHEP
  path and the HLS path still < 8 s.
- **Real hardware**: same `measure-latency.mjs` pointed at the live board
  page (it already reports per-sample OCR latency + playlist structure); add
  a `--whep` mode reading `getStats()`. Pis and viewer are NTP-synced, so the
  burned-in clock stays the cross-transport ground truth.

## 6. Proposed phasing (each a PR series, each shippable alone)

1. **P0 (done/awaiting deploy)**: 1 s GOP + 900 ms fragments (~5 s), 60 s DVR
   window + liveui (PRs #36/#15/#5).
2. **P1 — Pi pipeline**: 30 fps + superfast + leaky queues + sync=false.
   Cuts ~0.3–0.5 s from the HLS path today and is required groundwork.
   Watch Pi 5 CPU (x264 at 30 fps ≈ 1 core at superfast — verify on p29).
3. **P2 — mediamtx beside nginx-rtmp**: deb-wrap mediamtx into the apt repo;
   Pis dual-publish or mediamtx pulls; open UDP/TCP 8189 (tweed nftables +
   ten64 DNAT); WHEP behind the existing 443 proxy; extend CI harness.
4. **P3 — site**: WHEP live player + HLS-DVR swap controller on board pages;
   thumbnails unchanged.
5. **P4 — consolidation (optional)**: retire nginx-rtmp (mediamtx serves the
   HLS DVR too, or its record/playback API replaces it); RTSP ingest from Pis.

## 7. Open decisions for Tim

- Approve the mediamtx direction (vs. SRS "zero Pi changes, ~500 ms" or
  WebCodecs DIY)?
- 30 fps: OK with the Pi 5 CPU cost (superfast ≈1 core) and slightly higher
  bitrates? (Pi 4s are free — hardware encoder.)
- Which media port number to expose (8189 UDP+TCP suggested), and does a ten64
  DNAT for it need anything beyond the usual ansible/nft change?
- WHEP auth: leave streams public-read (matches HLS today) or gate via
  mediamtx JWT/HTTP hook?
- Rewind UX: is HLS-window scrubbing (P0) good enough, or is the
  frame-accurate mediamtx record/playback API (P4) worth it?
