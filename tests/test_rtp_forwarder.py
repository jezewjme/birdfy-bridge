"""Tests for the pure helpers in _rtp_forwarder.py.

Covers the NAL-splitting and SPS/PPS-caching logic that decides when the
passthrough ffmpeg is started and what gets prepended to IDR frames — all
exercisable without a real RTP stream. The forwarder coroutine itself needs a
live receiver + ffmpeg and is not covered here.
"""
import os

import pytest

pytest.importorskip("aiortc", reason="_rtp_forwarder imports aiortc at module load")

from _rtp_forwarder import (  # noqa: E402
    _START_CODE,
    NAL_IDR,
    NAL_PPS,
    NAL_SEI,
    NAL_SPS,
    _iter_nals,
    _ParamSetCache,
    _write_all,
)


def _nal(nal_type: int, payload: bytes = b"\x00") -> bytes:
    """Build a start-code-prefixed NAL of the given type."""
    header = bytes([nal_type & 0x1F])  # forbidden_zero_bit=0, nal_ref_idc=0
    return _START_CODE + header + payload


# ── _iter_nals ───────────────────────────────────────────────────────────────

def test_iter_nals_empty_and_garbage_yield_nothing():
    assert list(_iter_nals(b"")) == []
    # No Annex B start code anywhere — the "evicted keyframe head" signature.
    assert list(_iter_nals(b"\x12\x34\x56\x78" * 50)) == []


def test_iter_nals_single_nal():
    data = _nal(NAL_SPS, b"\x42\x00\x1f")
    out = list(_iter_nals(data))
    assert len(out) == 1
    nal_type, nal = out[0]
    assert nal_type == NAL_SPS
    assert nal == data  # includes the start code


def test_iter_nals_multiple_nals_in_order():
    data = _nal(NAL_SPS) + _nal(NAL_PPS) + _nal(NAL_IDR, b"\xab" * 20)
    types = [t for t, _ in _iter_nals(data)]
    assert types == [NAL_SPS, NAL_PPS, NAL_IDR]


def test_iter_nals_reassembles_full_bytes():
    parts = [_nal(NAL_SEI, b"\x01\x02"), _nal(NAL_IDR, b"\x03" * 10)]
    data = b"".join(parts)
    out = [nal for _, nal in _iter_nals(data)]
    assert out == parts
    assert b"".join(out) == data


def test_iter_nals_skips_leading_junk_before_first_start_code():
    data = b"\xde\xad\xbe\xef" + _nal(NAL_PPS)
    out = list(_iter_nals(data))
    assert len(out) == 1
    assert out[0][0] == NAL_PPS


def test_iter_nals_bare_start_code_yields_nothing():
    # A start code with no NAL header byte after it has no type to report.
    assert list(_iter_nals(_START_CODE)) == []


def test_iter_nals_masks_nal_type_bits():
    # nal_ref_idc bits (0x60) must not leak into the reported type.
    data = _START_CODE + bytes([0x65]) + b"\x00"  # 0x65 = ref_idc 3, type 5 (IDR)
    out = list(_iter_nals(data))
    assert out[0][0] == NAL_IDR


# ── _ParamSetCache ───────────────────────────────────────────────────────────

def test_param_set_cache_not_ready_until_both_seen():
    cache = _ParamSetCache()
    assert not cache.ready
    cache.observe(NAL_SPS, _nal(NAL_SPS))
    assert not cache.ready  # SPS alone is not enough
    cache.observe(NAL_PPS, _nal(NAL_PPS))
    assert cache.ready


def test_param_set_cache_ignores_other_nal_types():
    cache = _ParamSetCache()
    cache.observe(NAL_IDR, _nal(NAL_IDR))
    cache.observe(NAL_SEI, _nal(NAL_SEI))
    assert cache.sps is None
    assert cache.pps is None
    assert not cache.ready


def test_param_set_cache_keeps_latest():
    cache = _ParamSetCache()
    old_sps = _nal(NAL_SPS, b"\x01")
    new_sps = _nal(NAL_SPS, b"\x02")
    cache.observe(NAL_SPS, old_sps)
    cache.observe(NAL_SPS, new_sps)
    assert cache.sps == new_sps


# ── _write_all ───────────────────────────────────────────────────────────────

def test_write_all_writes_every_byte():
    r, w = os.pipe()
    try:
        payload = b"\xff" * 4096
        _write_all(w, payload)
        os.close(w)
        got = b""
        while True:
            chunk = os.read(r, 8192)
            if not chunk:
                break
            got += chunk
        assert got == payload
    finally:
        os.close(r)
