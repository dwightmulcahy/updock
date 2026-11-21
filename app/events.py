"""Server-Sent Events: fan out log entries and state changes from worker threads."""
import asyncio
import itertools
import json
import threading


class EventHub:
    def __init__(self, maxsize=1000):
        self._subs = set()
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self.maxsize = maxsize

    def subscribe(self):
        q = asyncio.Queue(self.maxsize)
        with self._lock:
            self._subs.add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs = {s for s in self._subs if s[1] is not q}

    @property
    def subscribers(self):
        return len(self._subs)

    def publish(self, kind, data):
        """Thread-safe; slow clients drop events rather than block updates."""
        item = (next(self._ids), kind, data)
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(_offer, q, item)
            except RuntimeError:  # loop closed: client is gone
                self.unsubscribe(q)


def _offer(q, item):
    if not q.full():
        q.put_nowait(item)


def format_event(item):
    eid, kind, data = item
    return f"id: {eid}\nevent: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


async def stream(hub, q, is_disconnected, heartbeat=15):
    """Yield SSE frames until the client disconnects."""
    try:
        yield "retry: 3000\n: connected\n\n"
        while not await is_disconnected():
            try:
                item = await asyncio.wait_for(q.get(), heartbeat)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield format_event(item)
    finally:
        hub.unsubscribe(q)
