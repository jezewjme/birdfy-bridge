FROM python:3.12-slim-bookworm

ARG TARGETARCH
ARG S6_OVERLAY_VERSION=3.2.0.2

# ffmpeg + build deps for aiortc (libav, opus, vpx, openssl) + xz-utils for s6 tarball extraction
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        gcc \
        g++ \
        libavcodec-dev \
        libavformat-dev \
        libavutil-dev \
        libswscale-dev \
        libswresample-dev \
        libopus-dev \
        libvpx-dev \
        libssl-dev \
        pkg-config \
        ca-certificates \
        curl \
        xz-utils \
    && rm -rf /var/lib/apt/lists/*

# --- s6-overlay (process supervisor) ------------------------------------------------
# Map Docker's TARGETARCH (amd64/arm64) to s6's arch names (x86_64/aarch64).
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) S6_ARCH=x86_64 ;; \
        arm64) S6_ARCH=aarch64 ;; \
        arm) S6_ARCH=armhf ;; \
        *) echo "unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz" \
        | tar -C / -Jxpf -; \
    curl -fsSL "https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz" \
        | tar -C / -Jxpf -

# --- MediaMTX (RTSP server) ---------------------------------------------------------
# Resolves the latest release tag at build time, then pins the download to it.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64) MTX_ARCH=linux_amd64 ;; \
        arm64) MTX_ARCH=linux_arm64v8 ;; \
        arm)   MTX_ARCH=linux_armv7 ;; \
        *) echo "unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    MTX_VERSION="$(curl -fsSL https://api.github.com/repos/bluenviron/mediamtx/releases/latest | sed -n 's/.*"tag_name":\s*"\([^"]*\)".*/\1/p')"; \
    echo "Installing MediaMTX ${MTX_VERSION} (${MTX_ARCH})"; \
    curl -fsSL -o /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}/mediamtx_${MTX_VERSION}_${MTX_ARCH}.tar.gz"; \
    tar -C /usr/local/bin -xzf /tmp/mediamtx.tar.gz mediamtx; \
    rm /tmp/mediamtx.tar.gz; \
    chmod +x /usr/local/bin/mediamtx

# --- Python deps --------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py /app/

# --- s6 service definitions + MediaMTX config ---------------------------------------
COPY docker/mediamtx.yml /etc/mediamtx.yml
COPY docker/s6-rc.d/ /etc/s6-overlay/s6-rc.d/

# Unbuffered stdout so logs appear immediately under s6
ENV PYTHONUNBUFFERED=1 \
    RTSP_HOST=localhost:8554 \
    RTSP_PATH=birdfy

EXPOSE 8554/tcp 8554/udp

ENTRYPOINT ["/init"]
