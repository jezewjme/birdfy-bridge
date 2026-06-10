# Changelog

All notable changes to this project will be documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Token refresh**: when a cached token is rejected, the bridge now tries a
  `refreshToken`-based renewal (`birdfy_api.refresh_token`) before falling back
  to a full login — avoiding Netvue's "new device logged in" email on token
  expiry. The refresh endpoint shape is unverified from captures, so it is
  best-effort (tries a few plausible URLs) with the full login as backstop.
  Disable with `NVS_NO_TOKEN_REFRESH=1`.
- **Audio**: the camera's PCMU (G.711 µ-law, 8 kHz mono) audio track is now
  muxed into the RTSP output via a second ffmpeg input with `-c:a copy` (no
  re-encode). POSIX-only (uses `pass_fds`); degrades to video-only elsewhere.
  Disable with `BIRDFY_AUDIO=0`.

### Fixed
- Removed conflicting duplicate definitions of `BIG_FRAME_BYTES` /
  `MAX_GARBAGE_DUMPS` in `_rtp_forwarder.py` (the later pair silently won).
- The parent process no longer leaks a file handle for ffmpeg's stderr log on
  every ffmpeg (re)start.
- The auth token cache file is created with `0600` permissions from the start
  (previously written, then chmodded — a brief default-umask window).
- `Ctrl-C` now exits cleanly instead of dumping a `KeyboardInterrupt` traceback.
- The integration smoke-test (`tests/test_api_integration.py`) used the
  websockets ≥14 `additional_headers` kwarg, which doesn't exist in the pinned
  `<14` legacy client; corrected to `extra_headers`.

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
