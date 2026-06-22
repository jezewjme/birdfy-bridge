"""Shared fake aiohttp.ClientSession for unit tests.

The cloud calls in birdfy_api all follow the same shape:

    async with aiohttp.ClientSession() as session:
        async with session.get/post(url, ...) as resp:
            text = await resp.text()
            resp.status ...

This helper lets a test queue up (status, body) responses; each get/post pops
the next one. Patch birdfy_api.aiohttp.ClientSession with `make_session_factory`.
"""
from __future__ import annotations


class FakeResp:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Pops a queued response per get/post; records the calls made.

    `responses` is a list of (status, body) tuples or Exception instances. An
    Exception is raised from the get/post call (to simulate a transport error).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # list of (method, url, kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self._responses:
            raise AssertionError(f"unexpected {method} {url} — no queued response")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return FakeResp(status, body)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)


def make_session_factory(responses):
    """Return a callable usable as a drop-in for aiohttp.ClientSession.

    All sessions created during the call share the same response queue, so a
    function that opens several `ClientSession()` contexts (e.g. login then
    get_devices) still pops responses in order.
    """
    session = FakeSession(responses)

    def factory(*a, **k):
        return session

    factory.session = session
    return factory
