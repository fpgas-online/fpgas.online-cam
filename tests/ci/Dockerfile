# End-to-end latency test environment: the same Debian packages the Pis and
# tweed run (nginx-rtmp, GStreamer + x264), plus real Google Chrome - the
# browser viewers actually use. NOT a Chromium build: Playwright's bundled
# Chromium has no H.264 at all (MEDIA_ERR_SRC_NOT_SUPPORTED on this stream),
# and a distro chromium is its own codec/WebRTC configuration, so a pass in
# either says little about Chrome. Plus tesseract to read the clockoverlay
# burned into every frame. Used by .github/workflows/e2e-latency.yml
# and for local runs (see tests/README.md). Multi-arch: works on amd64 runners
# and arm64 dev boxes alike.
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx-extras libnginx-mod-rtmp \
        gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly \
        gstreamer1.0-x \
        fonts-dejavu-core \
        tesseract-ocr tesseract-ocr-eng \
        nodejs npm python3 curl jq ca-certificates procps \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome stable, from Google's own deb (published for amd64 and arm64).
# apt resolves the deb's dependencies; the deb also adds Google's apt source.
RUN set -e; arch=$(dpkg --print-architecture); \
    curl -LsSf -o /opt/chrome.deb "https://dl.google.com/linux/direct/google-chrome-stable_current_${arch}.deb"; \
    apt-get update; apt-get install -y --no-install-recommends /opt/chrome.deb; \
    rm -rf /opt/chrome.deb /var/lib/apt/lists/*; \
    google-chrome --version

# playwright-core only: we drive the installed Chrome, no browser download.
WORKDIR /opt/e2e
RUN npm install --no-audit --no-fund playwright-core@1.55.0

# uv, as the repo convention for running Python (tests/ci/e2e.py is stdlib-only).
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

ENV NODE_PATH=/opt/e2e/node_modules \
    CHROME=/usr/bin/google-chrome
WORKDIR /src
