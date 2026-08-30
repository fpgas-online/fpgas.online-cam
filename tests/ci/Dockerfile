# End-to-end latency test environment: the same Debian packages the Pis and
# tweed run (nginx-rtmp, GStreamer + x264), plus a real H.264-capable browser
# (Debian's chromium; Playwright's bundled Chromium has no H.264 and reports
# MEDIA_ERR_SRC_NOT_SUPPORTED on this stream) and tesseract to read the
# clockoverlay burned into every frame. Used by .github/workflows/e2e-latency.yml
# and for local runs (see tests/README.md). Multi-arch: works on amd64 runners
# and arm64 dev boxes alike.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx-extras libnginx-mod-rtmp \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        chromium fonts-dejavu-core \
        tesseract-ocr tesseract-ocr-eng \
        nodejs npm python3 curl jq ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# playwright-core only: we drive the system chromium, no browser download.
WORKDIR /opt/e2e
RUN npm install --no-audit --no-fund playwright-core@1.55.0

# uv, as the repo convention for running Python (tests/ci/e2e.py is stdlib-only).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

ENV NODE_PATH=/opt/e2e/node_modules \
    CHROMIUM=/usr/bin/chromium
WORKDIR /src
