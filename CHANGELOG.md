# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — Initial public release

First public snapshot. The bridge has been used continuously against a real
Birdfy Feeder Bamboo for ~weeks before publication.

### What works
- Addx WebRTC path for `onAddx: true` devices (Birdfy Feeder Bamboo confirmed).
- Reverse-engineered Netvue `x-nvs-*` signed-header auth (login → device list →
  addx ticket → WebSocket signaling → SDP offer / answer + trickle ICE → H264).
- Camera-compat SDP rewrites (sctpmap injection, sha-384/512 fingerprint strip).
- Runtime aioice monkey-patches for camera STUN quirks (stale ufrag, single-shot
  nomination, DTLS gating).
- Docker image with bundled MediaMTX, s6-overlay supervision, multi-arch build
  (amd64 + arm64).
- Unit-test suite (NVS signature, SDP patches) — no network required.

### Not yet implemented
- KVS WebRTC path for `onAddx: false` devices.
- RTCP PLI on data-channel open to shorten the ~15s initial-keyframe wait.
- Token refresh (currently the bridge re-authenticates on disconnect).
- Audio mux into the RTSP output (video-only).

[Unreleased]: https://github.com/<owner>/birdfy-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/<owner>/birdfy-bridge/releases/tag/v0.1.0
