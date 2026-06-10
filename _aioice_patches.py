"""
Runtime monkey-patches for aioice that make the Addx camera handshake work.

These are load-bearing. Each one was identified by Wireshark capture against
both the working browser path and a broken bridge path; see the comments below
for the specific quirk being worked around. Importing this module installs the
patches as a side effect — import it once, before connecting.

If you're tempted to "clean these up" without consulting the pcaps:
  - 5-10-26 bridge connect.pcapng         — broken: camera hammers BINDING_REQs
  - 5-11-26 bridge connect.pcapng         — fixed: handshake completes
  - Birdify with Edge connecting          — reference: Chrome doing it right
"""
from __future__ import annotations

import logging

import aioice.ice
import aioice.stun as stun
from aioice.candidate import candidate_priority

logger = logging.getLogger(__name__)

# Connections whose remote_username has already been corrected — keyed by id()
# so we only log/update once per peer. id() is fine here because the set is
# only read+written from a single asyncio loop.
_ufrag_done: set[int] = set()

_orig_request_received = aioice.ice.Connection.request_received


def _patched_request_received(self, message, addr, protocol, raw_data):
    """Replacement for aioice.ice.Connection.request_received.

    Fixes three observed camera quirks:

      1. Wrong ufrag in outgoing BINDING_REQs (intermittent):
         Camera sometimes sends ``USERNAME='ours:stale_ufrag'`` where
         ``stale_ufrag`` is from a previous session and doesn't match its own
         SDP ufrag. Original aioice rejects with 400 and the camera never
         completes ICE on its side. We adopt the ufrag the camera is actually
         using so MESSAGE-INTEGRITY validates.

      2. aioice nominates once then stops (ice.py:850, 880-940):
         After our first USE-CANDIDATE check succeeds, aioice never sends
         another. If the camera missed or rejected that single check, it sits
         in "checking" forever, hammering us with BINDING_REQs at ~50ms
         intervals. THE HAMMER below fires a fresh BINDING_REQ +
         USE-CANDIDATE on every camera ping, sidestepping aioice's state
         machine entirely.

      3. Camera says ``setup:active`` but never sends DTLS ClientHello until
         ICE finishes its side. The ICE fix above lets that complete, then the
         camera honors its setup:active role and sends ClientHello as normal.
    """
    if message.message_method != stun.Method.BINDING:
        return _orig_request_received(self, message, addr, protocol, raw_data)

    # 1. Update remote ufrag on first mismatch (camera's stale-session bug)
    username = message.attributes.get("USERNAME", "")
    conn_id = id(self)
    if username and ":" in username and conn_id not in _ufrag_done:
        their_ufrag = username.split(":", 1)[1]
        if self.remote_username and self.remote_username != their_ufrag:
            logger.warning(
                f"aioice: ufrag mismatch — SDP={self.remote_username!r} "
                f"camera={their_ufrag!r}; updating remote_username"
            )
            self.remote_username = their_ufrag
            _ufrag_done.add(conn_id)

    # 2. Diagnostic: log pair state for this addr
    pair_state = "no-pair"
    for p in self._check_list:
        if p.remote_addr == addr:
            pair_state = (
                f"{p.state.name} nom={p.nominated} "
                f"task={'set' if p.task else 'none'}"
            )
            break
    logger.debug(
        f"camera REQ from {addr} user={username!r} "
        f"use_cand={'USE-CANDIDATE' in message.attributes} pair={pair_state}"
    )

    # 3. Always respond SUCCESS (signed with local_password — adds
    #    MESSAGE-INTEGRITY + FINGERPRINT)
    response = stun.Message(
        message_method=stun.Method.BINDING,
        message_class=stun.Class.RESPONSE,
        transaction_id=message.transaction_id,
    )
    response.attributes["XOR-MAPPED-ADDRESS"] = addr
    if self.local_password:
        response.add_message_integrity(self.local_password.encode("utf8"))
    protocol.send_stun(response, addr)

    # 4. THE HAMMER: fire a fresh USE-CANDIDATE on every camera ping. Bypasses
    #    aioice's "nominate once then stop" behavior.
    if self.ice_controlling and self.remote_username and self.remote_password:
        try:
            component = protocol.local_candidate.component
            req = stun.Message(
                message_method=stun.Method.BINDING,
                message_class=stun.Class.REQUEST,
            )
            req.attributes["USERNAME"] = f"{self.remote_username}:{self.local_username}"
            req.attributes["PRIORITY"] = candidate_priority(component, "prflx")
            req.attributes["ICE-CONTROLLING"] = self._tie_breaker
            req.attributes["USE-CANDIDATE"] = None  # flag attr
            req.add_message_integrity(self.remote_password.encode("utf8"))
            protocol.send_stun(req, addr)
        except Exception as e:
            logger.debug(f"hammer failed (non-fatal): {e}")

    # 5. Best-effort triggered-check (no-op once pair is SUCCEEDED)
    try:
        if self._check_list:
            self.check_incoming(message, addr, protocol)
    except Exception:
        pass


def install() -> None:
    """Install the patches. Idempotent."""
    if aioice.ice.Connection.request_received is not _patched_request_received:
        aioice.ice.Connection.request_received = _patched_request_received


# Install on import so callers only need ``import _aioice_patches``.
install()
