# Contributing

Thanks for considering a contribution! This is a small homelab tool, so the bar is "does it work for you against a real camera and not regress against mine". Issues and PRs welcome.

## Dev setup

```bash
git clone https://github.com/<owner>/birdfy-bridge.git
cd birdfy-bridge
python -m venv .venv
. .venv/bin/activate           # Linux/macOS
# or: .venv\Scripts\Activate.ps1   # Windows PowerShell
pip install -r requirements.txt -r requirements-dev.txt
```

## Running tests

```bash
pytest                          # unit tests, no network
pytest -m integration           # hits the real Netvue cloud — needs BIRDFY_EMAIL/BIRDFY_PASSWORD
```

Integration tests are opt-in via the `integration` mark and are not run in CI.

## Code style

```bash
ruff check .
ruff format --check .
```

The codebase prefers terse, technical writing in both code and prose. Comments should explain non-obvious *why*, not *what*. If you find yourself documenting what a well-named function does, consider renaming the function instead.

## Things that would be especially useful

- **More camera models.** If you have a Birdfy / Netvue device this bridge doesn't yet handle, open an issue with the device list output (with serials redacted) and we can work through the protocol.
- **KVS WebRTC path** (`onAddx: false` devices). This needs `boto3` + the AWS KVS WebRTC signaling client.
- **RTCP PLI on data-channel open** to shorten the ~15s wait for the first keyframe.
- **Audio mux** — the audio track is received but currently discarded.

## Protocol reverse-engineering

The wire protocol notes in [`docs/protocol.md`](docs/protocol.md) and [`birdfy_api.py`](birdfy_api.py) were derived by:

1. Logging into `my.birdfy.com` in Edge / Chrome and saving the network HAR.
2. Capturing the post-DTLS traffic with Wireshark to confirm SCTP / DTLS behavior.
3. Reading the web app's JavaScript bundles for header construction.

If you extend coverage to another device or endpoint, please add a similar paragraph in the relevant module's docstring describing how you confirmed it. "This worked once on my machine" is not enough; we need to know how a future maintainer can verify it again when Netvue changes something.

## What to avoid

- **Don't post your account credentials, session tokens, or device serial numbers** in issue reports. The DEBUG log redacts known secret fields but treat all logs as semi-sensitive — review before pasting.
- **Don't "clean up" the aioice or SDP patches** without re-reading the comments and ideally re-capturing a pcap to prove the patch is no longer needed.
- **Don't add this project to Netvue's forums, subreddits, or community channels.** Discoverability via search is fine; active promotion attracts disproportionate legal attention to a small interop tool.

## Reporting security issues

See [SECURITY.md](SECURITY.md).
