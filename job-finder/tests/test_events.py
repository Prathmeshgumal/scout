import asyncio

from job_finder.events import Hub, Recorder, follow


async def test_record_stores_and_publishes(store, hub, recorder):
    goal = await store.create_goal("g")
    queue = hub.subscribe(goal["id"])
    event = await recorder.record(goal["id"], "notice", text="hello")
    assert event["seq"] == 1
    assert queue.get_nowait() == event
    assert (await store.list_events(goal["id"]))[0]["text"] == "hello"


async def test_live_is_published_but_not_stored(store, hub, recorder):
    goal = await store.create_goal("g")
    queue = hub.subscribe(goal["id"])
    recorder.live(goal["id"], "text_delta", text="Hel")
    assert queue.get_nowait() == {"seq": None, "at": None, "type": "text_delta", "text": "Hel"}
    assert await store.list_events(goal["id"]) == []


async def test_follow_replays_after_n_then_follows_live_without_duplicates(store, hub, recorder):
    goal = await store.create_goal("g")
    await recorder.record(goal["id"], "user_message", text="one")
    await recorder.record(goal["id"], "text", text="two")
    stream = follow(store, hub, goal["id"], after=1)
    assert (await anext(stream))["text"] == "two"
    await recorder.record(goal["id"], "text", text="three")
    recorder.live(goal["id"], "text_delta", text="fo")
    assert (await asyncio.wait_for(anext(stream), 1))["seq"] == 3
    assert (await asyncio.wait_for(anext(stream), 1))["type"] == "text_delta"
    await stream.aclose()
    assert hub._subscribers == {}


async def test_follow_skips_a_stored_event_that_also_arrives_live(store, hub, recorder):
    goal = await store.create_goal("g")
    stream = follow(store, hub, goal["id"])
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    event = await recorder.record(goal["id"], "text", text="x")
    hub.publish(goal["id"], event)  # the same stored event delivered twice
    assert (await asyncio.wait_for(pending, 1))["seq"] == 1
    recorder.live(goal["id"], "text_delta", text="y")
    assert (await asyncio.wait_for(anext(stream), 1))["type"] == "text_delta"
    await stream.aclose()


async def test_follow_catches_up_from_the_store_after_an_overflow(store):
    hub = Hub(max_queue=2)
    recorder = Recorder(store, hub)
    goal = await store.create_goal("g")
    stream = follow(store, hub, goal["id"])
    pending = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    for n in range(1, 6):
        await recorder.record(goal["id"], "text", text=str(n))
    seqs = [(await asyncio.wait_for(pending, 1))["seq"]]
    for _ in range(4):
        seqs.append((await asyncio.wait_for(anext(stream), 1))["seq"])
    assert seqs == [1, 2, 3, 4, 5]
    await stream.aclose()


async def test_follow_with_poll_reads_events_that_another_process_stored(store, hub):
    goal = await store.create_goal("g")
    stream = follow(store, hub, goal["id"], poll_s=0.05)
    assert await asyncio.wait_for(anext(stream), 1) is None  # nothing new
    await store.append_event(goal["id"], "text", {"text": "from the CLI"})  # stored, not published here
    event = await asyncio.wait_for(anext(stream), 1)
    assert event["text"] == "from the CLI"
    await stream.aclose()


async def test_an_event_for_a_deleted_goal_is_dropped(store, recorder, hub):
    goal = await store.create_goal("g")
    queue = hub.subscribe(goal["id"])
    await store.delete_goals([goal["id"]])
    event = await recorder.record(goal["id"], "text", text="late")  # a late write of a stopped agent
    assert event["seq"] is None and queue.empty()


async def test_a_muted_goal_stores_and_shows_nothing(store, recorder, hub):
    goal = await store.create_goal("g")
    queue = hub.subscribe(goal["id"])
    with recorder.muted(goal["id"]):
        await recorder.record(goal["id"], "status_changed", status="stopped")
        recorder.live(goal["id"], "text_delta", text="…")
    await recorder.record(goal["id"], "text", text="after")
    assert [e["type"] for e in await store.list_events(goal["id"])] == ["text"]
    assert queue.get_nowait()["type"] == "text" and queue.empty()
