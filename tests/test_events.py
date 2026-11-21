import asyncio
import json

import pytest

from app.events import EventHub, format_event, stream


def run(coro):
    return asyncio.run(coro)


def test_format_event():
    assert format_event((7, "log", {"msg": "hi", "t": {1}})) == \
        'id: 7\nevent: log\ndata: {"msg": "hi", "t": "{1}"}\n\n'


def test_publish_delivers_to_subscribers_from_threads():
    async def main():
        hub = EventHub()
        q1, q2 = hub.subscribe(), hub.subscribe()
        assert hub.subscribers == 2
        await asyncio.to_thread(hub.publish, "state", {"name": "web"})
        a, b = await asyncio.wait_for(q1.get(), 1), await asyncio.wait_for(q2.get(), 1)
        hub.unsubscribe(q1)
        assert hub.subscribers == 1
        return a, b
    a, b = run(main())
    assert a == b and a[1:] == ("state", {"name": "web"})


def test_full_queue_drops_events():
    async def main():
        hub = EventHub(maxsize=1)
        q = hub.subscribe()
        hub.publish("log", 1)
        hub.publish("log", 2)
        await asyncio.sleep(0)
        return q.qsize(), (await q.get())[2]
    assert run(main()) == (1, 1)


def test_closed_loop_unsubscribes():
    hub = EventHub()

    async def sub():
        return hub.subscribe()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(sub())
    loop.close()
    hub.publish("log", 1)
    assert hub.subscribers == 0


def test_stream_events_heartbeat_and_disconnect():
    async def main():
        hub = EventHub()
        q = hub.subscribe()
        hub.publish("log", {"msg": "a"})
        state = {"n": 0}

        async def disconnected():
            state["n"] += 1
            return state["n"] > 3
        frames = [f async for f in stream(hub, q, disconnected, heartbeat=0.01)]
        return frames, hub.subscribers
    frames, subs = run(main())
    assert frames[0].startswith("retry: 3000")
    assert frames[1].startswith("id: 1\nevent: log\n") and json.loads(frames[1].split("data: ")[1]) == {"msg": "a"}
    assert ": ping\n\n" in frames[2:]
    assert subs == 0  # unsubscribed in finally


@pytest.mark.parametrize("n", [0])
def test_stream_immediate_disconnect(n):
    async def main():
        hub = EventHub()
        q = hub.subscribe()

        async def disconnected():
            return True
        return [f async for f in stream(hub, q, disconnected)], hub.subscribers
    frames, subs = run(main())
    assert len(frames) == 1 and subs == 0
