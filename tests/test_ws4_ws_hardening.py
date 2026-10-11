"""WS4 — WebSocket server-side hardening (v6.34.0): broadcast_ws sends to all
clients concurrently (no per-client head-of-line); chat-history jsonl parsing
runs off the event loop. Server-side only (no frontend change)."""

from __future__ import annotations

import asyncio


def test_broadcast_is_concurrent_not_head_of_line():
    """A slow / half-open client must NOT delay the broadcast (and the heartbeat)
    to every other client — gather, not a sequential await loop."""
    from ouroboros.gateway import ws as wsmod

    order: list = []

    class _Client:
        def __init__(self, name, delay):
            self.name = name
            self.delay = delay

        async def send_text(self, data):
            await asyncio.sleep(self.delay)
            order.append(self.name)

    async def _run():
        slow = _Client("slow", 0.20)
        fast = _Client("fast", 0.0)
        wsmod._ws_clients[:] = [slow, fast]  # slow registered FIRST
        await wsmod.broadcast_ws({"type": "x"})

    try:
        asyncio.run(_run())
        # The fast client completes before the slow one despite being second:
        # proof the sends ran concurrently, not sequentially head-of-line.
        assert order == ["fast", "slow"]
    finally:
        wsmod._ws_clients[:] = []


def test_broadcast_drops_only_failed_clients():
    """A client whose send raises is dropped; healthy clients still receive."""
    from ouroboros.gateway import ws as wsmod

    received: list = []

    class _Good:
        async def send_text(self, data):
            received.append("good")

    class _Bad:
        async def send_text(self, data):
            raise RuntimeError("broken pipe")

    async def _run():
        good, bad = _Good(), _Bad()
        wsmod._ws_clients[:] = [bad, good]
        await wsmod.broadcast_ws({"type": "y"})
        return good, bad

    try:
        good, bad = asyncio.run(_run())
        assert received == ["good"]
        # The broken client was removed from the registry; the good one stays.
        assert bad not in wsmod._ws_clients
        assert good in wsmod._ws_clients
    finally:
        wsmod._ws_clients[:] = []
