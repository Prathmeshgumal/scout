"""Uploaded files: types, text, storage, routes and the tools of the agents. All resume text here is made up."""

import base64
import io
import json
import logging

import httpx
import pytest
from docx import Document
from pypdf import PdfWriter

from job_finder.career_lens import CareerLens
from job_finder.deletion import Deleter
from job_finder.files import MAX_BYTES, FileRefused, FileStorageError, FileStore, classify, clean_name, extract_text
from job_finder.mandate_store import MandateStore
from job_finder.tools import ToolContext
from job_finder.tools.artifacts import build_tools
from job_finder.web.app import create_app
from job_finder.web.sessions import GoalSessions

from .fakes import FakeObjects, Streams, make_services, replies
from tests.conftest import signed_in_client

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
RESUME_TEXT = "Fake Person wrote Go services for ten years."


def blank_pdf() -> bytes:
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.write(out)
    return out.getvalue()


def word(text: str) -> bytes:
    document = Document()
    document.add_paragraph(text)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


@pytest.fixture
def objects():
    return FakeObjects()


@pytest.fixture
def files(pool, objects):
    return FileStore(pool, objects)


@pytest.fixture
async def goal(store):
    return await store.create_goal("g")


def ctx_for(goal, store, recorder, files):
    return ToolContext(goal["id"], store, recorder, CareerLens("", ""), files=files)


def tool(ctx, name, agent="main"):
    return next(t for t in build_tools(ctx, agent) if t.name == name).handler


@pytest.mark.parametrize("name, data, kind", [
    ("cv.pdf", b"%PDF-1.7 x", "pdf"), ("a.png", PNG, "image"), ("a.JPG", b"\xff\xd8\xff\xe0x", "image"),
    ("a.gif", b"GIF89a x", "image"), ("a.webp", b"RIFF\x00\x00\x00\x00WEBPx", "image"),
    ("notes.md", "héllo".encode(), "text"), ("cv.docx", b"PK\x03\x04x", "docx"), ("a.zip", b"PK\x03\x04", "other"),
])
def test_a_file_type_comes_from_its_name_and_bytes(name, data, kind):
    assert classify(name, data)[0] == kind


@pytest.mark.parametrize("name, data", [
    ("cv.pdf", b"<html>"), ("a.png", b"GIF89a"), ("a.jpg", PNG), ("a.webp", b"RIFF0000WAVE"), ("cv.docx", b"%PDF-"),
    ("notes.md", b"a\x00b"), ("notes.txt", b"\xff\xfe\xfa"),
])
def test_a_name_that_does_not_match_the_bytes_is_refused(name, data):
    with pytest.raises(FileRefused):
        classify(name, data)


def test_a_name_loses_its_folders_and_control_characters():
    assert clean_name("../../etc/pass\nwd.txt") == "passwd.txt"
    assert clean_name("C:\\Users\\me\\cv.pdf") == "cv.pdf"
    assert clean_name("") == "file"
    assert clean_name(None) == "file"


def test_text_comes_from_word_and_text_files():
    assert extract_text("docx", word(RESUME_TEXT)) == RESUME_TEXT
    assert extract_text("text", b"  hello\n") == "hello"


def test_a_pdf_with_no_text_and_a_broken_file_give_no_text():
    assert extract_text("pdf", blank_pdf()) is None
    assert extract_text("pdf", b"%PDF-broken") is None
    assert extract_text("docx", b"PK\x03\x04broken") is None
    assert extract_text("image", PNG) is None


async def test_a_file_keeps_its_bytes_and_its_facts(files, objects):
    row = await files.save("notes.md", b"hello")
    assert (row["role"], row["kind"], row["size_bytes"], row["readable"]) == ("attachment", "text", 5, True)
    assert "text" not in row and "object_key" not in row
    assert await files.data(row["id"]) == b"hello"
    assert list(objects.saved.values()) == [b"hello"]


async def test_an_empty_or_large_file_is_refused(files, objects):
    with pytest.raises(FileRefused, match="empty"):
        await files.save("a.md", b"")
    with pytest.raises(FileRefused, match="larger"):
        await files.save("a.md", b"x" * (MAX_BYTES + 1))
    assert objects.saved == {}


async def test_a_new_resume_replaces_the_old_one(files, objects):
    first = await files.save("old.md", b"old", "resume")
    second = await files.save("new.md", b"new", "resume")
    assert (await files.resume())["id"] == second["id"]
    assert await files.get(first["id"]) is None
    assert list(objects.saved.values()) == [b"new"]


async def test_no_storage_refuses_an_upload(pool):
    with pytest.raises(FileRefused, match="not set up"):
        await FileStore(pool, None).save("a.md", b"x")


async def test_storage_that_is_down_gives_a_clear_error_and_no_row(files, objects):
    objects.down = True
    with pytest.raises(FileStorageError):
        await files.save("a.md", b"x")
    assert await files.resume() is None


async def test_an_attachment_stays_with_its_first_goal(files, store, goal):
    other = await store.create_goal("other")
    row = await files.save("a.md", b"x")
    assert [r["id"] for r in await files.link([row["id"]], goal["id"])] == [row["id"]]
    assert await files.link([row["id"]], other["id"]) == []
    assert (await files.get(row["id"]))["goal_id"] == goal["id"]


async def test_a_resume_cannot_become_an_attachment(files, goal):
    row = await files.save("cv.md", b"x", "resume")
    assert await files.link([row["id"]], goal["id"]) == []


async def test_deleting_a_goal_removes_its_files_and_keeps_the_resume(files, objects, store, recorder, goal, app_settings):
    row = await files.save("a.md", b"x")
    await files.link([row["id"]], goal["id"])
    resume = await files.save("cv.md", b"cv", "resume")
    await Deleter(app_settings, store, MandateStore(store.pool), recorder, files=files).delete_goal(goal["id"])
    assert await files.get(row["id"]) is None
    assert await files.get(resume["id"]) is not None
    assert list(objects.saved.values()) == [b"cv"]


# The tools


async def test_list_files_shows_the_files_of_this_goal_only(files, store, recorder, goal):
    other = await store.create_goal("other")
    mine, theirs = await files.save("mine.md", b"m"), await files.save("theirs.md", b"t")
    await files.link([mine["id"]], goal["id"])
    await files.link([theirs["id"]], other["id"])
    await files.save("cv.md", b"cv", "resume")
    reply = (await tool(ctx_for(goal, store, recorder, files), "list_files")({}))["content"][0]["text"]
    assert "mine.md" in reply and "cv.md" in reply and "theirs.md" not in reply


async def test_read_file_gives_the_text_of_an_attachment(files, store, recorder, goal):
    row = await files.save("a.md", b"the text")
    await files.link([row["id"]], goal["id"])
    reply = await tool(ctx_for(goal, store, recorder, files), "read_file")({"file_id": row["id"]})
    assert reply["content"][0]["text"] == "the text"


async def test_read_file_gives_an_image_as_an_image(files, store, recorder, goal):
    row = await files.save("a.png", PNG)
    await files.link([row["id"]], goal["id"])
    reply = await tool(ctx_for(goal, store, recorder, files), "read_file")({"file_id": row["id"]})
    block = reply["content"][1]
    assert (block["type"], block["mimeType"]) == ("image", "image/png")
    assert base64.b64decode(block["data"]) == PNG


async def test_read_file_refuses_a_file_of_another_goal(files, store, recorder, goal):
    other = await store.create_goal("other")
    row = await files.save("a.md", b"secret")
    await files.link([row["id"]], other["id"])
    read = tool(ctx_for(goal, store, recorder, files), "read_file")
    for file_id in (row["id"], "not-an-id"):
        reply = await read({"file_id": file_id})
        assert reply.get("is_error") and "secret" not in reply["content"][0]["text"]


async def test_only_candidate_profile_reads_the_resume_as_a_file(files, store, recorder, goal):
    row = await files.save("cv.md", RESUME_TEXT.encode(), "resume")
    ctx = ctx_for(goal, store, recorder, files)
    refused = await tool(ctx, "read_file", "main")({"file_id": row["id"]})
    assert refused.get("is_error") and RESUME_TEXT not in refused["content"][0]["text"]
    assert (await tool(ctx, "read_file", "candidate-profile")({"file_id": row["id"]}))["content"][0]["text"] == RESUME_TEXT


async def test_a_scanned_pdf_tells_the_agent_to_ask_for_the_text(files, store, recorder, goal):
    row = await files.save("cv.pdf", blank_pdf())
    await files.link([row["id"]], goal["id"])
    reply = await tool(ctx_for(goal, store, recorder, files), "read_file")({"file_id": row["id"]})
    assert reply.get("is_error") and "paste" in reply["content"][0]["text"]


async def test_get_brief_gives_the_uploaded_resume_only_to_the_agents_that_need_it(files, store, recorder, goal):
    await store.set_profile(goal["id"], {"summary": "a"}, ready=True)
    await files.save("cv.md", RESUME_TEXT.encode(), "resume")
    ctx = ctx_for(goal, store, recorder, files)
    assert RESUME_TEXT in (await tool(ctx, "get_brief", "fit-check")({}))["content"][0]["text"]
    reply = (await tool(ctx, "get_brief", "main")({}))["content"][0]["text"]
    assert RESUME_TEXT not in reply and '"has_resume": true' in reply


async def test_file_text_stays_out_of_the_log(files, caplog):
    caplog.set_level(logging.DEBUG)
    await files.save("cv.md", RESUME_TEXT.encode(), "resume")
    assert RESUME_TEXT not in caplog.text


# The routes


@pytest.fixture
def streams():
    return Streams(replies("On it."))


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


@pytest.fixture
async def sessions(services):
    manager = GoalSessions(services, idle_s=600)
    yield manager
    await manager.close_all()


@pytest.fixture
async def client(app_settings, services, sessions):
    app = create_app(app_settings, services, sessions)
    async with signed_in_client(app) as client:
        yield client


def upload(client, name="notes.md", data=b"hello", role="attachment"):
    return client.post("/api/files", files={"file": (name, data)}, data={"role": role})


async def test_an_upload_gives_the_facts_and_no_text(client):
    response = await upload(client)
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["kind"], body["readable"]) == ("notes.md", "text", True)
    assert not {"text", "object_key"} & set(body)


async def test_a_bad_upload_says_why(client, services):
    assert (await upload(client, "cv.pdf", b"not a pdf")).status_code == 400
    assert "not a real PDF" in (await upload(client, "cv.pdf", b"not a pdf")).json()["detail"]
    assert (await upload(client, data=b"x" * (MAX_BYTES + 1))).status_code == 413
    assert (await upload(client, role="other")).status_code == 422
    services.files.objects.down = True
    assert (await upload(client)).status_code == 503


async def test_a_download_is_an_attachment_that_the_browser_does_not_run(client):
    file_id = (await upload(client, "pa\"ge.html", b"<script>1</script>")).json()["id"]
    response = await client.get(f"/api/files/{file_id}/download")
    assert response.content == b"<script>1</script>"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"].startswith("text/plain")


async def test_the_list_gives_the_resume_and_the_files_of_a_goal(client, streams):
    await upload(client, "cv.md", b"cv", "resume")
    attached = (await upload(client)).json()["id"]
    goal_id = (await client.post("/api/goals", json={"text": "Find jobs", "file_ids": [attached]})).json()["goal_id"]
    body = (await client.get("/api/files", params={"goal_id": goal_id})).json()
    assert body["resume"]["name"] == "cv.md"
    assert [row["id"] for row in body["files"]] == [attached]
    assert streams.last.queries == ["Find jobs\n\nAttached files: notes.md"]
    assert (await client.get("/api/goals")).json()[0]["title"] == "Find jobs"


async def test_a_message_can_attach_files(client, streams, store):
    goal = await store.create_goal("g")
    await store.set_status(goal["id"], "done")
    attached = (await upload(client, "a.md")).json()["id"]
    response = await client.post(f"/api/goals/{goal['id']}/messages", json={"text": "Read it", "file_ids": [attached]})
    assert response.status_code == 202
    assert streams.last.queries[-1] == "Read it\n\nAttached files: a.md"


async def test_a_file_id_must_be_a_uuid(client):
    assert (await client.post("/api/goals", json={"text": "x", "file_ids": ["nope"]})).status_code == 422
    assert (await client.get("/api/files/nope/download")).status_code == 404


async def test_a_deleted_file_is_gone(client):
    file_id = (await upload(client)).json()["id"]
    assert (await client.delete(f"/api/files/{file_id}")).status_code == 204
    assert (await client.delete(f"/api/files/{file_id}")).status_code == 404
    assert (await client.get(f"/api/files/{file_id}/download")).status_code == 404


async def test_file_text_stays_out_of_the_events(client, store, streams):
    attached = (await upload(client, "a.md", RESUME_TEXT.encode())).json()["id"]
    goal_id = (await client.post("/api/goals", json={"text": "Go", "file_ids": [attached]})).json()["goal_id"]
    assert RESUME_TEXT not in json.dumps(await store.list_events(goal_id), default=str)
