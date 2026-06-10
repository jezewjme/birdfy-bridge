# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.0] — 2026-06-10

Initial public release. The bridge had been running continuously against a
real Birdfy Feeder Bamboo for several weeks before publication.

### What works
- Addx WebRTC path for `onAddx: true` devices (Birdfy Feeder Bamboo confirmed).
- Reverse-engineered Netvue `x-nvs-*` signed-header auth (login → device list →
  addx ticket → WebSocket signaling → SDP offer / answer + trickle ICE → H264).
- **Token caching and refresh**: auth tokens are cached to disk (created `0600`),
  and when a cached token is rejected the bridge tries a `refreshToken`-based
  renewal (`birdfy_api.refresh_token`) before falling back to a full login —
  avoiding Netvue's "new device logged in" email on token expiry. The refresh
  endpoint shape is unverified from captures, so it is best-effort (tries a few
  plausible URLs) with the full login as backstop. Disable with
  `NVS_NO_TOKEN_REFRESH=1`.
- **RTP passthrough**: H264 RTP frames are forwarded to ffmpeg without
  transiting aiortc's decoder (`_rtp_forwarder.py`), with SPS/PPS caching,
  keyframe-gated ffmpeg startup, and PLI/FIR keyframe nudging.
- **aiortc media patches** (`_aiortc_media_patches.py`): jitter-buffer widening,
  longer NACK history, and re-NACK on keyframe loss — fixes the garbage
  keyframes caused by aiortc's 128-packet jitter-buffer eviction.
- **Audio**: the camera's PCMU (G.711 µ-law, 8 kHz mono) audio track is muxed
  into the RTSP output via a second ffmpeg input with `-c:a copy` (no
  re-encode). POSIX-only (uses `pass_fds`); degrades to video-only elsewhere.
  Disable with `BIRDFY_AUDIO=0`.
- Camera-compat SDP rewrites (sctpmap injection, sha-384/512 fingerprint strip).
- Runtime aioice monkey-patches for camera STUN quirks (stale ufrag, single-shot
  nomination, DTLS gating).
- Stepped reconnect backoff (2 s → 5 min cap, resets after any stable session).
- Docker image with bundled MediaMTX (version pinned via the `MEDIAMTX_VERSION`
  build arg), s6-overlay supervision, a publish-aware container healthcheck,
  and multi-arch build (amd64 + arm64).
- Unit-test suite (NVS signature, SDP patches, RTP forwarder) — no network
  required.

### Not yet implemented
- KVS WebRTC path for `onAddx: false` devices.

[Unreleased]: https://github.com/jezewjme/birdfy-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jezewjme/birdfy-bridge/releases/tag/v0.1.0
