# Pointing Frigate at the bridge

If Frigate runs on the same Docker network: `rtsp://birdfy-bridge:8554/birdfy`.

From another host: `rtsp://<host-running-birdfy-bridge>:8554/birdfy`.

You can bypass the bundled MediaMTX entirely and publish straight to Frigate's go2rtc by setting `RTSP_OUTPUT=rtsp://frigate:8554/birdfy` in `.env`.

## Recommended setup: hardware-decode the full-res stream, let Frigate downscale

The bridge outputs a single **1920×1080** H264 stream. The CPU concern with `detect` is the **decode**, not the resolution: if Frigate *software*-decodes 1080p for every analyzed frame, ffmpeg CPU spikes (you'll see a "high FFmpeg CPU usage" warning). The fix is to **hardware-decode** the full-res stream and let Frigate downscale to the detect size on the GPU. No separate substream is needed.

Point both `detect` and `record` at the one `birdfy` stream, give the camera `hwaccel_args`, and set `detect: width/height` to the small analysis size — Frigate decodes once on the iGPU and scales there:

```yaml
go2rtc:
  # Keep the bridge stream warm so detect recovers in ~1s when the feeder camera
  # wakes from sleep, instead of waiting for a reactive respawn (avoids the
  # record-maintainer "unprocessed recording segments" stall). Needs go2rtc ≥ 1.9.11.
  preload:
    birdfy:
  streams:
    # Single connection to the bridge. #audio=aac transcodes the bridge's PCMU
    # audio to AAC so Frigate's MP4 record container can hold it (MP4 can't store
    # pcm_mulaw). Drop the #audio=aac modifier if you record video-only.
    birdfy:
      - "ffmpeg:rtsp://birdfy-bridge:8554/birdfy#video=copy#audio=aac"

cameras:
  BirdfyFeeder:
    ffmpeg:
      hwaccel_args: preset-vaapi   # Intel VAAPI; use your platform's preset
      inputs:
        # Detect: full-res stream, hardware-decoded and downscaled to the detect
        # width/height on the GPU. No go2rtc substream / re-encode.
        - path: rtsp://127.0.0.1:8554/birdfy
          input_args: preset-rtsp-restream
          roles: [detect]
        # Record: same full-resolution stream (no quality loss on recordings).
        - path: rtsp://127.0.0.1:8554/birdfy
          input_args: preset-rtsp-restream
          roles: [record]
    detect:
      width: 640
      height: 360
      fps: 5
```

**Why not a go2rtc detect substream?** An earlier version of this guide recommended a downscaled `birdfy_sub` stream (`ffmpeg:birdfy#video=h264#hardware#width=640#height=360`). That works, but it makes go2rtc **decode → scale → re-encode** the stream, which (a) does the GPU work *twice* — go2rtc re-encodes, then Frigate decodes the substream again — and (b) the re-encode hop is fragile on this camera's bursty, NACK-recovered link: every transcode hiccup surfaced to Frigate as `RTP: PT=60: bad cseq` / "exceeded fps limit" watchdog teardowns and a 404 restart cascade. Hardware-decoding the full-res stream directly is **one** GPU decode pass, **zero** re-encodes, and removes that failure mode entirely. Measured on an Intel N305: detect ffmpeg ~0.9% CPU and iGPU near idle — comparable to the substream's detect cost without the extra encode. If your host has *no* hardware decoder, the `birdfy_sub` re-encode approach is still a reasonable fallback to keep software decode off the full 1080p frame.

Reload the Frigate config after editing — it doesn't auto-reload.
