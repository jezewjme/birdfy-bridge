"""
SDP rewriting helpers for the Addx camera handshake.

Pure functions only — no I/O, no aiortc state. Kept separate so they can be
unit-tested without spinning up an RTCPeerConnection. Every patch here exists
to work around a specific quirk in the camera's SDP parser or DTLS/SCTP stack;
do not "clean these up" without consulting the pcap evidence referenced in the
comments.
"""
from __future__ import annotations


def inject_sctpmap(sdp: str) -> tuple[str, bool]:
    """Inject ``a=sctpmap:5000 webrtc-datachannel 1024`` after ``a=sctp-port:5000``.

    The camera's SDP parser requires the legacy ``a=sctpmap`` line alongside
    the modern ``a=sctp-port``. Without it, SDP_ANSWER never returns.

    Returns ``(new_sdp, patched)``.
    """
    if "a=sctp-port:5000" not in sdp or "a=sctpmap:5000" in sdp:
        return sdp, False
    patched = sdp.replace(
        "a=sctp-port:5000\r\n",
        "a=sctp-port:5000\r\na=sctpmap:5000 webrtc-datachannel 1024\r\n",
    )
    return patched, True


def strip_non_sha256_fingerprints(sdp: str) -> tuple[str, int]:
    """Drop ``a=fingerprint:sha-384`` / ``sha-512`` lines from the offer.

    The camera's SDP parser picks one fingerprint line and verifies all DTLS
    records against that hash. aiortc emits sha-256, sha-384, and sha-512 by
    default; if the camera picks a non-sha-256 line, every DTLS record after
    the handshake is silently dropped (including the SCTP INIT) and the stream
    never starts. Browsers ship only sha-256, so we mirror that.

    Returns ``(new_sdp, lines_removed)``.
    """
    lines = sdp.split("\r\n")
    kept = [
        ln for ln in lines
        if not ln.startswith("a=fingerprint:sha-384")
        and not ln.startswith("a=fingerprint:sha-512")
    ]
    removed = len(lines) - len(kept)
    if removed == 0:
        return sdp, 0
    return "\r\n".join(kept), removed


def extract_trickle_candidates(sdp: str) -> list[dict]:
    """Parse ``a=candidate:`` lines for trickling over the signaling WebSocket.

    With MAX_BUNDLE the same candidates appear under each m= section, but the
    browser only trickles candidates with ``sdpMLineIndex=0``; we mirror that.

    Returns a list of ``{candidate, sdpMid, sdpMLineIndex, ufrag}`` dicts in
    SDP order.
    """
    out: list[dict] = []
    sdp_mline = -1
    sdp_mid = "0"
    ufrag = ""
    for raw_line in sdp.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            sdp_mline += 1
            sdp_mid = str(sdp_mline)
        elif line.startswith("a=mid:"):
            sdp_mid = line[len("a=mid:"):].strip()
        elif line.startswith("a=ice-ufrag:"):
            ufrag = line[len("a=ice-ufrag:"):].strip()
        elif line.startswith("a=candidate:") and sdp_mline == 0:
            cand_no_prefix = line[len("a="):]
            out.append({
                "candidate": cand_no_prefix,
                "sdpMid": sdp_mid,
                "sdpMLineIndex": 0,
                "ufrag": ufrag,
            })
    return out


def apply_offer_patches(sdp: str) -> tuple[str, dict]:
    """Apply every camera-compatibility rewrite to a local SDP offer.

    Returns ``(patched_sdp, info)`` where ``info`` records what changed so the
    caller can log it. ``info`` keys: ``sctpmap_injected`` (bool),
    ``fingerprints_stripped`` (int).
    """
    sdp, sctpmap_injected = inject_sctpmap(sdp)
    sdp, fingerprints_stripped = strip_non_sha256_fingerprints(sdp)
    return sdp, {
        "sctpmap_injected": sctpmap_injected,
        "fingerprints_stripped": fingerprints_stripped,
    }
