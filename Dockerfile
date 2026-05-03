FROM python:3.12-slim-bookworm

# ffmpeg + build deps for aiortc (libav, opus, vpx, openssl)
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
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

# Unbuffered stdout so logs appear immediately
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
