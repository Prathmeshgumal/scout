"""The job pipeline: the user moves roles through the stages and keeps notes."""

import httpx
import pytest

from job_finder.jobs import role_key
from job_finder.pipeline import PipelineStore
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions

from .fakes import Streams, make_services, replies


async def add_job(store, goal_id, title="Backend Engineer", company="Acme", location="Pune", fit="fit", n=1):
    async with store.pool.connection() as conn:
        row = await (await conn.execute("""
            INSERT INTO jobs (goal_id, job_key, role_key, url, source, title, company_name, location, evidence_level,
                              fit, fit_reasons, fit_count, created_by)
            VALUES (%s, %s, %s, %s, 'linkedin_company', %s, %s, %s, 'full_description', %s, 'Matches Go.', 0, 'test')
            RETURNING id::text AS id, role_key
        """, (goal_id, f"key-{goal_id}-{n}", role_key(company, title, location), f"https://jobs.test/{n}", title, company,
              location, fit))).fetchone()
    return row


@pytest.fixture
def pipeline(store):
    return PipelineStore(store.pool)


@pytest.fixture
async def goal(store):
    return await store.create_goal("Go roles")


async def stages(pipeline):
    return {item["title"]: item["stage"] for item in await pipeline.items()}


async def test_a_fit_role_starts_in_fit_jobs(pipeline, store, goal):
    await add_job(store, goal["id"])
    await add_job(store, goal["id"], title="Other", fit="partial", n=2)
    await add_job(store, goal["id"], title="No verdict", fit=None, n=3)
    assert await stages(pipeline) == {"Backend Engineer": "fit"}


async def test_the_user_moves_a_role_through_the_stages(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    for stage in ("referral", "applied", "responded", "fit"):
        await pipeline.set_stage(job["role_key"], stage)
        assert await stages(pipeline) == {"Backend Engineer": stage}


async def test_the_user_can_add_a_role_that_is_not_a_fit(pipeline, store, goal):
    job = await add_job(store, goal["id"], fit="partial")
    await pipeline.set_stage(job["role_key"], "applied")
    assert await stages(pipeline) == {"Backend Engineer": "applied"}


async def test_a_removed_role_stays_out_and_can_come_back(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    await pipeline.set_stage(job["role_key"], "removed")
    assert await pipeline.items() == []
    await pipeline.set_stage(job["role_key"], "fit")
    assert await stages(pipeline) == {"Backend Engineer": "fit"}


async def test_a_removed_role_stays_out_when_another_goal_finds_it(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    await pipeline.set_stage(job["role_key"], "removed")
    await add_job(store, (await store.create_goal("again"))["id"], n=2)
    assert await pipeline.items() == []


async def test_a_role_from_two_goals_shows_once(pipeline, store, goal):
    await add_job(store, goal["id"])
    second = await store.create_goal("second")
    await add_job(store, second["id"], n=2)
    assert len(await pipeline.items()) == 1


async def test_a_fit_verdict_wins_over_a_not_fit_of_another_goal(pipeline, store, goal):
    await add_job(store, goal["id"], fit="not_fit")
    await add_job(store, (await store.create_goal("second"))["id"], fit="fit", n=2)
    assert [item["fit"] for item in await pipeline.items()] == ["fit"]


async def test_an_unknown_role_gives_none(pipeline):
    assert await pipeline.set_stage("nothing|here|", "applied") is None
    assert await pipeline.add_note("nothing|here|", "text") is None


async def test_notes_stay_with_the_role_in_order(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    first = await pipeline.add_note(job["role_key"], "Asked Priya for a referral")
    await pipeline.add_note(job["role_key"], "Applied on the site")
    await pipeline.set_stage(job["role_key"], "applied")
    item = (await pipeline.items())[0]
    assert [note["text"] for note in item["notes"]] == ["Asked Priya for a referral", "Applied on the site"]
    assert await pipeline.delete_note(first["id"]) is True
    assert await pipeline.delete_note(first["id"]) is False
    assert len((await pipeline.items())[0]["notes"]) == 1


async def test_a_note_on_a_fit_role_keeps_it_in_fit_jobs(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    await pipeline.add_note(job["role_key"], "Looks good")
    assert await stages(pipeline) == {"Backend Engineer": "fit"}


async def test_a_tracked_role_and_its_notes_survive_the_delete_of_the_goal(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    await pipeline.set_stage(job["role_key"], "applied")
    await pipeline.add_note(job["role_key"], "Applied on Monday")
    await store.delete_goals([goal["id"]])
    item = (await pipeline.items())[0]
    assert (item["title"], item["company_name"], item["stage"]) == ("Backend Engineer", "Acme", "applied")
    assert item["url"] == "https://jobs.test/1"
    assert item["job_id"] is None and item["goal_id"] is None
    assert [note["text"] for note in item["notes"]] == ["Applied on Monday"]
    await pipeline.set_stage(job["role_key"], "responded")
    assert await stages(pipeline) == {"Backend Engineer": "responded"}


async def test_an_untracked_fit_role_goes_with_its_goal(pipeline, store, goal):
    await add_job(store, goal["id"])
    await store.delete_goals([goal["id"]])
    assert await pipeline.items() == []


async def test_the_item_links_to_the_role_and_its_goal(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    item = (await pipeline.items())[0]
    assert (item["job_id"], item["goal_id"], item["goal_title"]) == (job["id"], goal["id"], "Go roles")
    assert item["role_key"] == job["role_key"] and item["fit_reasons"] == "Matches Go."


async def test_a_role_with_any_verdict_can_join_the_pipeline(pipeline, store, goal):
    for n, fit in enumerate(("partial", "unsure", "not_fit", None)):
        job = await add_job(store, goal["id"], title=f"Role {fit}", fit=fit, n=n)
        assert (await pipeline.set_stage(job["role_key"], "fit"))["stage"] == "fit"
        assert await pipeline.stage_of(job["role_key"]) == "fit"


async def test_a_removed_role_stays_on_the_board_with_its_notes_and_can_return(pipeline, store, goal):
    job = await add_job(store, goal["id"])
    await pipeline.add_note(job["role_key"], "Asked a friend")
    await pipeline.set_stage(job["role_key"], "removed")
    board = await pipeline.board()
    assert board["items"] == []
    assert [(r["role_key"], r["stage"]) for r in board["removed"]] == [(job["role_key"], "removed")]
    assert board["removed"][0]["notes"][0]["text"] == "Asked a friend"
    await pipeline.set_stage(job["role_key"], "fit")
    board = await pipeline.board()
    assert board["removed"] == [] and board["items"][0]["notes"][0]["text"] == "Asked a friend"


# The routes


@pytest.fixture
async def client(app_settings, store, hub, recorder):
    services = make_services(app_settings, store, hub, recorder, Streams(replies("On it.")))
    manager = GoalSessions(services, idle_s=600)
    app = create_app(app_settings, services, manager)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    await manager.close_all()


async def test_the_routes_move_a_role_and_keep_notes(client, store, goal):
    job = await add_job(store, goal["id"])
    moved = await client.put("/api/pipeline/stage", json={"role_key": job["role_key"], "stage": "applied"})
    assert moved.status_code == 200 and moved.json()["stage"] == "applied"
    note = await client.post("/api/pipeline/notes", json={"role_key": job["role_key"], "text": "  Sent  "})
    assert note.status_code == 201 and note.json()["text"] == "Sent"
    body = (await client.get("/api/pipeline")).json()
    assert body["items"][0]["notes"][0]["text"] == "Sent" and body["removed"] == []
    assert (await client.delete(f"/api/pipeline/notes/{note.json()['id']}")).status_code == 204
    assert (await client.delete(f"/api/pipeline/notes/{note.json()['id']}")).status_code == 404


async def test_the_routes_refuse_bad_input(client, store, goal):
    job = await add_job(store, goal["id"])
    key = job["role_key"]
    assert (await client.put("/api/pipeline/stage", json={"role_key": key, "stage": "hired"})).status_code == 422
    assert (await client.put("/api/pipeline/stage", json={"role_key": "no|such|", "stage": "applied"})).status_code == 404
    assert (await client.post("/api/pipeline/notes", json={"role_key": key, "text": "   "})).status_code == 422
    assert (await client.post("/api/pipeline/notes", json={"role_key": "no|such|", "text": "x"})).status_code == 404
    assert (await client.post("/api/pipeline/notes", json={"role_key": key, "text": "x" * 5001})).status_code == 422
    assert (await client.delete("/api/pipeline/notes/nope")).status_code == 404


async def test_the_detail_of_a_role_gives_its_stage(client, store, goal):
    job = await add_job(store, goal["id"], fit="partial")
    url = f"/api/goals/{goal['id']}/jobs/{job['id']}"
    body = (await client.get(url)).json()
    assert body["pipeline"] == {"role_key": job["role_key"], "stage": None}
    await client.put("/api/pipeline/stage", json={"role_key": job["role_key"], "stage": "referral"})
    assert (await client.get(url)).json()["pipeline"]["stage"] == "referral"
