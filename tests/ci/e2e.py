#!/usr/bin/env python3
"""End-to-end latency test: gst-libcam.sh -> nginx-rtmp (HLS) -> video.js in Chromium.

Runs the repo's real publisher script (gst-libcam.sh, with CAM_SRC pointed at
videotestsrc and RTMP_DEST at a local nginx-rtmp), waits for the HLS playlist,
then runs tests/measure-latency.mjs against tests/ci/player.html and fails if
the median glass-to-glass latency exceeds --max-latency.

Everything runs unprivileged from a scratch prefix; nothing is installed.
Intended to run inside the tests/ci/Dockerfile image (see tests/README.md):

    uv run tests/ci/e2e.py [--max-latency 8] [--samples 5] [--keep]
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / "tests" / "ci"
HTTP = "http://127.0.0.1:18080"
RTMP = "rtmp://127.0.0.1:11935/pib/test"


def wait_for(what: str, fn, timeout: float, interval: float = 0.5):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if fn():
                print(f"[e2e] {what}: ok after {time.monotonic() - t0:.1f}s", flush=True)
                return
        except Exception:  # noqa: BLE001 - polling; any failure just means "not yet"
            pass
        time.sleep(interval)
    raise SystemExit(f"[e2e] timed out after {timeout}s waiting for {what}")


def http_get(path: str) -> str:
    with urllib.request.urlopen(HTTP + path, timeout=5) as r:
        return r.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-latency", type=float, default=8.0, help="fail if median latency (s) exceeds this")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--keep", action="store_true", help="keep the scratch prefix (logs, HLS files)")
    ap.add_argument("--fps", type=int, default=6, help="FPS passed to gst-libcam.sh")
    ap.add_argument("--gop", type=int, default=None, help="GOP passed to gst-libcam.sh (default: FPS, i.e. 1 s)")
    args = ap.parse_args()

    prefix = Path(tempfile.mkdtemp(prefix="cam-e2e-"))
    for d in ("hls", "www", "tmp"):
        (prefix / d).mkdir()
    shutil.copy(CI / "player.html", prefix / "www" / "player.html")
    conf = prefix / "nginx.conf"
    conf.write_text((CI / "nginx.conf").read_text().replace("@PREFIX@", str(prefix)))
    print(f"[e2e] prefix {prefix}", flush=True)

    procs: list[subprocess.Popen] = []
    try:
        nginx = subprocess.Popen(["nginx", "-p", str(prefix), "-c", str(conf)],
                                 stdout=(prefix / "nginx.out").open("w"), stderr=subprocess.STDOUT)
        procs.append(nginx)
        wait_for("nginx http", lambda: http_get("/player.html").startswith("<!DOCTYPE"), 15)

        env = dict(os.environ,
                   CAM_SRC="videotestsrc is-live=true pattern=ball ! video/x-raw,width=1280,height=720",
                   RTMP_DEST=RTMP, FPS=str(args.fps), GOP=str(args.gop or args.fps))
        cam = subprocess.Popen(["bash", str(REPO / "gst-libcam.sh")], env=env,
                               stdout=(prefix / "cam.out").open("w"), stderr=subprocess.STDOUT)
        procs.append(cam)
        # Playlist appears once the first fragment closes; needs >= 3 entries for VHS to play live.
        wait_for("HLS playlist with 3 fragments",
                 lambda: cam.poll() is None and http_get("/live/test.m3u8").count("#EXTINF") >= 3, 60)
        m3u8 = http_get("/live/test.m3u8")
        print("[e2e] playlist:\n" + m3u8, flush=True)
        with urllib.request.urlopen(HTTP + "/live/test.m3u8", timeout=5) as r:
            cc = r.headers.get("Cache-Control")
        print(f"[e2e] playlist Cache-Control: {cc}", flush=True)
        if cc != "no-cache":
            raise SystemExit("[e2e] playlist is not served Cache-Control: no-cache")

        report = prefix / "latency.json"
        cmd = ["node", str(REPO / "tests" / "measure-latency.mjs"),
               f"{HTTP}/player.html?src=/live/test.m3u8",
               "--samples", str(args.samples), "--max-latency", str(args.max_latency),
               "--json", str(report), "--video", "#tt-video"]
        print("[e2e] " + " ".join(cmd), flush=True)
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[e2e] FAILED (measure-latency exit {rc})", flush=True)
            for f in ("cam.out", "nginx.out", "error.log"):
                p = prefix / f
                if p.exists():
                    print(f"----- {f} (tail)\n" + "\n".join(p.read_text().splitlines()[-40:]), flush=True)
        else:
            print("[e2e] PASSED", flush=True)
        return rc
    finally:
        for p in reversed(procs):
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
        for p in procs:
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
        if args.keep:
            print(f"[e2e] kept {prefix}", flush=True)
        else:
            shutil.rmtree(prefix, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
