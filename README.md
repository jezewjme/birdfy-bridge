# birdfy-bridge

An interoperability tool that converts a Birdfy / Netvue camera's WebRTC stream into RTSP so it can be consumed by Frigate, go2rtc, VLC, Home Assistant, or anything else that speaks RTSP.

> **Unofficial, unaffiliated client.** Not endorsed by, sponsored by, or affiliated with Netvue Inc., Birdfy, Vicohome, or Addx. "Birdfy", "Netvue", "Vicohome", and "Addx" are trademarks of their respective owners and are used here only nominatively to identify the cameras this tool interoperates with. See [Disclaimer](#disclaimer) for terms-of-service and warranty notes.

## Status

**Working** — the Birdfy Feeder Bamboo streams reliably into Frigate over RTSP via the bundled MediaMTX. The WebRTC handshake, keyframe recovery, and timestamp handling are all confirmed against live captures. Known rough edges are listed under [Limitations and caveats](docs/operations.md#limitations-and-caveats).

## Supported cameras

- **Birdfy Feeder Bamboo** (confirmed)
- Other Addx-based Birdfy / Netvue cameras (`onAddx: true` in the device list) should work; please open an issue if you try one.
- Newer KVS-based devices (`onAddx: false`) are not yet supported — see [Limitations and caveats](docs/operations.md#limitations-and-caveats).

## Quick start (Docker)

```bash
git clone https://github.com/<owner>/birdfy-bridge.git
cd birdfy-bridge
cp .env.example .env
# edit .env with your BIRDFY_EMAIL, BIRDFY_PASSWORD, DEVICE_ID
docker compose up --build -d
docker compose logs -f
```

Then point any RTSP client at `rtsp://<docker-host>:8554/birdfy` (VLC: Media → Open Network Stream).

The container bundles [MediaMTX](https://github.com/bluenviron/mediamtx) as an in-container RTSP server, so it works standalone — no external RTSP infra required.

The three variables in `.env` are all you need to start:

| Env var          | Required | Description |
|------------------|----------|-------------|
| `BIRDFY_EMAIL`   | Yes      | Netvue / Birdfy account email |
| `BIRDFY_PASSWORD`| Yes      | Account password (plain text, MD5'd internally) |
| `DEVICE_ID`      | No       | Camera serial number; defaults to the first device on the account. See [Finding your DEVICE_ID](docs/configuration.md#finding-your-device_id). |

Every other option (logging, RTSP output, token persistence, keyframe-recovery tunables, MQTT) is documented in **[Configuration](docs/configuration.md)**.

## Documentation

| Guide | What's in it |
|-------|--------------|
| [Configuration](docs/configuration.md) | Full env-var reference · finding your `DEVICE_ID` · token persistence |
| [Pointing Frigate at the bridge](docs/frigate.md) | RTSP URLs · the recommended hardware-decode setup · go2rtc preload |
| [Home Assistant control & sensors](docs/home-assistant.md) | MQTT setup · the three modes (`always_on` / `auto` / `off`) · battery sensor |
| [How it works](docs/protocol.md) | Architecture diagrams · the reverse-engineered Addx/KVS protocol · NVS signing · the SDP/ICE/RTP quirks and why they're load-bearing |
| [Operations](docs/operations.md) | Limitations & caveats · the container healthcheck |
| [Contributing](CONTRIBUTING.md) | Dev setup · running tests · protocol reverse-engineering notes |
| [Security policy](SECURITY.md) | Reporting vulnerabilities |

## Security warnings

- **`.env` contains your full Birdfy account password.** Anyone with that file can log into your account and reach every camera, doorbell, or other Netvue device on it. Keep it out of backups, repos, and shared drives. Use a dedicated Birdfy account for the bridge if at all possible.
- **Credential storage is a known limitation.** The bridge currently caches the login token locally (see [Token persistence](docs/configuration.md#token-persistence)) rather than via an OS keychain or secrets manager. It's written `0600` and git-ignored, but treat the state volume as sensitive — a leaked token grants account access until it expires. Delete the file (or the volume) to force a fresh login. **Roadmap:** pluggable secrets-manager backend (e.g. `keyring`, Vault) for the cached token.
- **The bundled MediaMTX has no authentication.** This is fine on a trusted LAN. **Do not expose port 8554 to the public internet** — anyone who can reach it can pull your stream. If you must, configure `publishUser` / `readUser` in `docker/mediamtx.yml` and put the container behind a reverse proxy.
- **DEBUG logs may still contain non-secret identifying data** (device serials, region info, ICE server URLs). Bearer tokens, signed URLs, refresh tokens, and AWS credentials are redacted, but treat DEBUG logs as semi-sensitive when sharing them in bug reports.
- **Netvue's API is reverse-engineered, not contracted.** It can change at any time. The bridge fails loudly when it does; please open an issue if your account suddenly stops authenticating or device-list returns 4xx.

## Development

```bash
python -m venv .venv && . .venv/bin/activate   # Linux/macOS
# or: .venv\Scripts\Activate.ps1                # Windows PowerShell
pip install -r requirements.txt -r requirements-dev.txt
pytest                   # unit tests, no network
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for more.

## Dependencies

### Python (`requirements.txt`)
- `aiortc` — WebRTC library (handles SDP, ICE, H264 depayload). Patched at runtime; see [RTP receive-path quirks](docs/protocol.md#rtp-receive-path-quirks).
- `aiohttp` — async HTTP for API calls
- `websockets` — WebSocket client for signaling. Works on both the legacy (`<14`) and modern asyncio (`>=14`) APIs — the `extra_headers`→`additional_headers` rename is handled at runtime, so the version is unpinned across that boundary.
- `aiomqtt` — optional; only used when `MQTT_HOST` is set (see [Home Assistant control & sensors](docs/home-assistant.md)).
- `av`, `numpy` — pulled in by aiortc (we don't decode video ourselves; the decoder is stubbed to a no-op)

### System (Docker image installs these)
- `ffmpeg` — H264 passthrough (`-c copy`) + push RTSP (no re-encode)
- `mediamtx` — bundled RTSP server (latest release fetched at build time)
- `s6-overlay` — process supervisor for running mediamtx + bridge together

## Disclaimer

This project is for use with cameras **you own**. Using it may violate Netvue's Terms of Service and could result in your Birdfy account being suspended or terminated. The Netvue API is reverse-engineered (see [How it works](docs/protocol.md)) and can change or break at any time without notice. The project ships with no warranty (see [LICENSE](LICENSE)).

## License

MIT — see [LICENSE](LICENSE).
