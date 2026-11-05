"""The write path, and the ordering that keeps a row from naming a missing blob.

In the tool this replaces, 39 of 202 stored keys pointed at objects that did
not exist and nothing detected it. The fix is ordering, not a reconciliation
sweep: the object is written and verified first, so a row naming a missing
object cannot be created.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from career_lens.capture.writer import BlobMissing, Document, write_capture
from career_lens.store.ids import REQUEST, new_id

NOW = datetime.now(UTC)


class FakeStore:
    """An in-memory blob store. Two knobs, both modelling real failures."""

    def __init__(self, *, swallow: bool = False, fail_after: int | None = None):
        self.objects: dict[str, bytes] = {}
        # `swallow` accepts a put and keeps nothing: an upload that reports
        # success and stores nothing is precisely what produced the dangling
        # keys, so it has to be reproducible here.
        self.swallow = swallow
        # Counted rather than matched on the key. A blob key is
        # `captures/{capture_id}/...` and carries no section, so there is nothing
        # in it to match a particular document on -- and capture ids are
        # base32hex, whose alphabet has no "x", so a substring like
        # "experience" can never appear in one.
        self.fail_after = fail_after
        self.puts = 0

    def put(self, key, data, content_type):
        self.puts += 1
        if self.fail_after is not None and self.puts > self.fail_after:
            raise RuntimeError("upload failed")
        if not self.swallow:
            self.objects[key] = data

    def get(self, key):
        return self.objects[key]

    def head(self, key):
        return len(self.objects[key]) if key in self.objects else None

    def url(self, key, expires=3600):
        return f"https://blobs.invalid/{key}"


@pytest.fixture
def request_id(app) -> str:
    rid = new_id(REQUEST)
    app.execute(text("""
        INSERT INTO scrape_request
            (id, url_requested, url_normalised, kind, status, requested_at)
        VALUES (:r, :u, :u, 'linkedin_person', 'running', :t)
    """), {"r": rid, "u": "https://www.linkedin.com/in/jane/", "t": NOW})
    return rid


def _document(section="profile", html=b"<html><body>hi</body></html>",
              screenshots=(b"png-one",)) -> Document:
    return Document(
        section=section,
        url_requested="https://www.linkedin.com/in/jane/",
        url_final="https://www.linkedin.com/in/jane/",
        host="www.linkedin.com",
        http_status=200,
        captured_at=NOW,
        title="Jane",
        html=html,
        screenshots=list(screenshots),
        markdown="# Jane",
        markdown_parser="docling-4",
    )


def test_a_capture_and_its_artifacts_are_written(app_url, app, request_id):
    store = FakeStore()
    capture_id = write_capture(app_url, request_id=request_id,
                               document=_document(), store=store,
                               browser_profile="research")
    assert app.execute(text(
        "SELECT section FROM capture WHERE id = :c"), {"c": capture_id}
    ).scalar_one() == "profile"
    kinds = app.execute(text(
        "SELECT kind FROM artifact WHERE capture_id = :c ORDER BY kind"),
        {"c": capture_id}).scalars().all()
    assert kinds == ["html", "screenshot"]


def test_complete_rendered_text_is_a_verified_artifact(
        app_url, app, request_id):
    document = _document()
    document.text = "complete rendered text"
    store = FakeStore()

    capture_id = write_capture(
        app_url, request_id=request_id, document=document, store=store)

    assert store.objects[f"captures/{capture_id}/page.txt"] == (
        b"complete rendered text")
    row = app.execute(text("""
        SELECT kind, content_type, byte_size, sha256
        FROM artifact WHERE capture_id=:capture AND kind='text'
    """), {"capture": capture_id}).one()
    assert row.kind == "text"
    assert row.content_type == "text/plain; charset=utf-8"
    assert row.byte_size == len(b"complete rendered text")


def test_capture_metadata_is_persisted_for_later_extraction(
        app_url, app, request_id):
    document = _document()
    document.metadata = {"has_more": True}

    capture_id = write_capture(
        app_url, request_id=request_id, document=document, store=FakeStore(),
        browser_profile="research")

    assert app.execute(text(
        "SELECT metadata FROM capture WHERE id = :c"),
        {"c": capture_id}).scalar_one() == {"has_more": True}


def test_the_blob_exists_before_the_row_naming_it(app_url, app, request_id):
    store = FakeStore()
    capture_id = write_capture(app_url, request_id=request_id,
                               document=_document(), store=store,
                               browser_profile="research")
    keys = app.execute(text(
        "SELECT blob_key FROM artifact WHERE capture_id = :c"),
        {"c": capture_id}).scalars().all()
    for key in keys:
        assert store.head(key) is not None, f"{key} is named by a row and absent"


def test_a_silently_dropped_upload_refuses_to_write_the_row(app_url, app, request_id):
    # The measured failure, reproduced: put() succeeds, the object is not there.
    store = FakeStore(swallow=True)
    with pytest.raises(BlobMissing):
        write_capture(app_url, request_id=request_id, document=_document(),
                      store=store, browser_profile="research")
    assert app.execute(text("SELECT count(*) FROM capture")).scalar_one() == 0


def test_a_capture_is_queryable_the_moment_write_capture_returns(
        app_url, app, request_id):
    """One document, one transaction. This is what lets a run be watched: the
    profile page is readable while the sections are still being fetched."""
    store = FakeStore()

    first = write_capture(app_url, request_id=request_id,
                          document=_document(section="profile"), store=store,
                          browser_profile="research")
    after_first = app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id = :r"),
        {"r": request_id}).scalar_one()

    second = write_capture(app_url, request_id=request_id,
                           document=_document(section="experience"), store=store,
                           browser_profile="research")

    assert after_first == 1
    assert first != second
    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id = :r"),
        {"r": request_id}).scalar_one() == 2


def test_a_failure_part_way_through_keeps_the_captures_already_landed(
        app_url, app, request_id):
    """Each document is its own unit of work now, so a run that dies on its
    fourth page keeps the three before it. That is correct under the three
    layers: a capture is what the site sent, and a later failure does not make
    it untrue. What must still never happen is a half-written document.

    Two uploads per document (html + one screenshot), so failing after the
    second lands on the second document's first upload.
    """
    store = FakeStore(fail_after=2)
    with pytest.raises(RuntimeError):
        for document in (_document(), _document(section="experience")):
            write_capture(app_url, request_id=request_id, document=document,
                          store=store, browser_profile="research")

    assert app.execute(text("SELECT count(*) FROM capture")).scalar_one() == 1
    assert app.execute(text(
        "SELECT section FROM capture")).scalar_one() == "profile"
    # The one that landed is whole: no capture row without its artifacts.
    assert app.execute(text("SELECT count(*) FROM artifact")).scalar_one() == 2


def test_banded_screenshots_keep_their_page_order(app_url, app, request_id):
    store = FakeStore()
    capture_id = write_capture(
        app_url, request_id=request_id,
        document=_document(screenshots=(b"top", b"middle", b"bottom")),
        store=store, browser_profile="research")
    rows = app.execute(text("""
        SELECT ordinal, blob_key FROM artifact
        WHERE capture_id = :c AND kind = 'screenshot' ORDER BY ordinal
    """), {"c": capture_id}).all()
    assert [r.ordinal for r in rows] == [0, 1, 2]
    assert [store.get(r.blob_key) for r in rows] == [b"top", b"middle", b"bottom"]


def test_the_recorded_digest_is_of_what_was_stored(app_url, app, request_id):
    from career_lens.store.blobs import sha256

    store = FakeStore()
    html = b"<html><body>exact bytes</body></html>"
    capture_id = write_capture(app_url, request_id=request_id,
                               document=_document(html=html), store=store,
                               browser_profile="research")
    row = app.execute(text("""
        SELECT blob_key, sha256, byte_size FROM artifact
        WHERE capture_id = :c AND kind = 'html'
    """), {"c": capture_id}).one()
    assert row.sha256 == sha256(html)
    assert row.byte_size == len(html)
    assert store.get(row.blob_key) == html


def test_navigation_metadata_is_persisted_with_the_capture(app_url, app, request_id):
    store = FakeStore()
    document = _document()
    document.request_method = "GET"
    document.request_headers = {"accept": "text/html"}
    document.response_headers = {"content-type": "text/html"}

    capture_id = write_capture(
        app_url, request_id=request_id, document=document, store=store,
        browser_profile="research")

    row = app.execute(text("""
        SELECT request_method, request_headers, response_headers
        FROM capture WHERE id = :c
    """), {"c": capture_id}).one()
    assert row.request_method == "GET"
    assert row.request_headers == {"accept": "text/html"}
    assert row.response_headers == {"content-type": "text/html"}


def test_a_capture_keeps_whether_it_was_read_to_the_end(app_url, app, request_id):
    """A feed that stopped early must not look like one that finished.

    The Posts and Life tabs of a company page have no end to reach, so they are
    bounded rather than followed. Two captures of the same feed -- one complete,
    one cut off at the budget -- hold the same kind of bytes and differ only
    here.
    """
    document = _document(section="posts")
    document.scroll_stopped = "budget"
    document.scroll_growing = True
    document.screenshot_error = "Timeout 30000ms exceeded"

    capture_id = write_capture(app_url, request_id=request_id,
                               document=document, store=FakeStore(),
                               browser_profile="research")

    row = app.execute(text("""
        SELECT scroll_stopped, scroll_growing, screenshot_error
        FROM capture WHERE id = :c
    """), {"c": capture_id}).one()
    assert row.scroll_stopped == "budget"
    assert row.scroll_growing is True
    assert row.screenshot_error == "Timeout 30000ms exceeded"


def test_a_capture_read_to_the_end_says_so_rather_than_saying_nothing(
        app_url, app, request_id):
    """The default is 'not growing', not NULL. NULL would be ambiguous."""
    capture_id = write_capture(app_url, request_id=request_id,
                               document=_document(), store=FakeStore(),
                               browser_profile="research")

    row = app.execute(text("""
        SELECT scroll_growing, screenshot_error FROM capture WHERE id = :c
    """), {"c": capture_id}).one()
    assert row.scroll_growing is False
    assert row.screenshot_error is None


# ---- the page's own images, beside the screenshots -------------------------

def _media(content_type="image/jpeg", data=b"\xff\xd8\xff-jpeg"):
    from career_lens.capture.media import Media
    return Media(data=data, content_type=content_type, url="https://media.invalid/x")


def test_a_profile_photo_and_cover_are_stored_as_artifacts(
        app_url, app, request_id):
    document = _document()
    document.images = {
        "profile_image": _media(data=b"avatar-bytes"),
        "cover_image": _media(content_type="image/png", data=b"cover-bytes"),
    }
    store = FakeStore()

    capture_id = write_capture(app_url, request_id=request_id,
                               document=document, store=store)

    rows = app.execute(text("""
        SELECT kind, content_type, byte_size, blob_key
        FROM artifact WHERE capture_id = :c AND kind LIKE '%image'
        ORDER BY kind
    """), {"c": capture_id}).all()
    assert [(row.kind, row.content_type, row.byte_size) for row in rows] == [
        ("cover_image", "image/png", len(b"cover-bytes")),
        ("profile_image", "image/jpeg", len(b"avatar-bytes")),
    ]
    # In the same bucket and under the same prefix as the screenshots.
    assert {row.blob_key for row in rows} == {
        f"captures/{capture_id}/cover-image.png",
        f"captures/{capture_id}/profile-image.jpg",
    }
    assert store.objects[f"captures/{capture_id}/profile-image.jpg"] == b"avatar-bytes"


def test_an_image_is_verified_in_the_bucket_before_its_row_exists(
        app_url, app, request_id):
    """The ordering that the whole write path exists to guarantee."""
    document = _document(screenshots=())
    document.images = {"profile_image": _media()}

    with pytest.raises(BlobMissing):
        write_capture(app_url, request_id=request_id, document=document,
                      store=FakeStore(swallow=True))

    assert app.execute(text(
        "SELECT count(*) FROM artifact WHERE kind = 'profile_image'"),
    ).scalar_one() == 0


def test_a_capture_with_no_images_writes_none(app_url, app, request_id):
    capture_id = write_capture(app_url, request_id=request_id,
                               document=_document(), store=FakeStore())

    assert app.execute(text(
        "SELECT count(*) FROM artifact WHERE capture_id = :c "
        "AND kind LIKE '%image'"), {"c": capture_id}).scalar_one() == 0


def test_a_har_is_stored_as_its_own_verified_artifact(app_url, app, request_id):
    document = _document()
    document.har = b"\x1f\x8b" + b"gzipped-har-bytes"
    store = FakeStore()

    capture_id = write_capture(
        app_url, request_id=request_id, document=document, store=store)

    key = f"captures/{capture_id}/network.har.gz"
    assert store.objects[key] == document.har
    row = app.execute(text("""
        SELECT kind, content_type, byte_size FROM artifact
        WHERE capture_id=:c AND kind='har'
    """), {"c": capture_id}).one()
    assert row.content_type == "application/gzip"
    assert row.byte_size == len(document.har)


def test_a_capture_without_a_har_writes_no_har_row(app_url, app, request_id):
    """Request logging is optional, and off must mean absent, not empty."""
    capture_id = write_capture(
        app_url, request_id=request_id, document=_document(), store=FakeStore())

    assert app.execute(text(
        "SELECT count(*) FROM artifact WHERE capture_id=:c AND kind='har'"),
        {"c": capture_id}).scalar_one() == 0


def test_a_har_that_cannot_be_stored_leaves_no_rows(app_url, app, request_id):
    """The ordering guarantee has to cover the HAR like every other payload."""
    document = _document()
    document.har = b"gzipped-har-bytes"

    with pytest.raises(BlobMissing):
        write_capture(app_url, request_id=request_id, document=document,
                      store=FakeStore(swallow=True))

    assert app.execute(text(
        "SELECT count(*) FROM capture WHERE request_id = :r"),
        {"r": request_id}).scalar_one() == 0
