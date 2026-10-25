"""The HAR recorder: what it keeps, what it refuses, and what it redacts.

Nothing here touches the network. The recorder only ever sees Playwright's
event objects, so fakes standing in for those exercise every decision it makes.
"""

from __future__ import annotations

import base64
import gzip
import json

import pytest

from career_lens.capture.har import (
    BODY_MAX_BYTES,
    BODY_STORE_MAX_BYTES,
    CAPTURE_BODY_BUDGET,
    HarRecorder,
    registrable,
)


class FakePage:
    def __init__(self, url: str):
        self.url = url


class FakeFrame:
    def __init__(self, page: FakePage):
        self.page = page


class FakeRequest:
    def __init__(self, url, *, method="GET", resource_type="fetch",
                 headers=None, page_url="https://www.linkedin.com/in/x/",
                 failure=None):
        self.url = url
        self.method = method
        self.resource_type = resource_type
        self.headers = headers or {"user-agent": "Chrome", "accept": "*/*"}
        self.frame = FakeFrame(FakePage(page_url))
        self.failure = failure
        self.timing = {}


class FakeResponse:
    def __init__(self, request, *, status=200, status_text="OK",
                 headers=None, body=b"", raises=None):
        self.request = request
        self.status = status
        self.status_text = status_text
        self.headers = headers or {}
        self.url = request.url
        self._body = body
        self._raises = raises
        self.body_calls = 0

    def body(self):
        self.body_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._body


class FakeContext:
    """Records handlers so a test can drive the recorder's event sequence."""

    def __init__(self):
        self.handlers: dict[str, list] = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        self.handlers.get(event, []).remove(handler)

    def emit(self, event, payload):
        for handler in list(self.handlers.get(event, [])):
            handler(payload)


def exchange(context, request, response=None):
    """One full request lifecycle, the way Playwright dispatches it."""
    context.emit("request", request)
    if response is not None:
        context.emit("response", response)
    context.emit("requestfinished", request)


def har_of(blob: bytes) -> dict:
    return json.loads(gzip.decompress(blob).decode("utf-8"))


@pytest.fixture
def recorder():
    return HarRecorder()


@pytest.fixture
def context(recorder):
    ctx = FakeContext()
    recorder.attach(ctx)
    return ctx


def json_headers(size: int) -> dict[str, str]:
    return {"content-type": "application/json", "content-length": str(size)}


# -- shape -------------------------------------------------------------


def test_take_produces_valid_har_1_2(recorder, context):
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    exchange(context, request,
             FakeResponse(request, headers=json_headers(2), body=b"{}"))

    log = har_of(recorder.take("profile"))["log"]

    assert log["version"] == "1.2"
    entry = log["entries"][0]
    # HAR 1.2 requires all of these on every entry.
    for field in ("startedDateTime", "time", "request", "response", "cache",
                  "timings"):
        assert field in entry, field
    for field in ("method", "url", "httpVersion", "cookies", "headers",
                  "queryString", "headersSize", "bodySize"):
        assert field in entry["request"], field
    for field in ("status", "statusText", "headers", "content", "redirectURL",
                  "headersSize", "bodySize"):
        assert field in entry["response"], field
    assert entry["pageref"] == log["pages"][0]["id"]


def test_take_returns_none_when_nothing_happened(recorder, context):
    assert recorder.take("profile") is None


def test_take_drains_so_captures_do_not_overlap(recorder, context):
    first = FakeRequest("https://www.linkedin.com/a")
    exchange(context, first, FakeResponse(first))
    recorder.take("profile")

    second = FakeRequest("https://www.linkedin.com/b")
    exchange(context, second, FakeResponse(second))

    entries = har_of(recorder.take("skills"))["log"]["entries"]
    assert [entry["request"]["url"] for entry in entries] == [
        "https://www.linkedin.com/b"]


def test_a_request_still_in_flight_waits_for_the_next_capture(recorder, context):
    """Recording it early as a failure would be worse than recording it late."""
    slow = FakeRequest("https://www.linkedin.com/slow")
    context.emit("request", slow)

    assert recorder.take("profile") is None

    exchange(context, slow, FakeResponse(slow))
    entries = har_of(recorder.take("skills"))["log"]["entries"]
    assert entries[0]["request"]["url"] == "https://www.linkedin.com/slow"


def test_a_request_that_never_responded_is_status_zero(recorder, context):
    failed = FakeRequest("https://www.linkedin.com/gone", failure="net::ERR")
    context.emit("request", failed)
    context.emit("requestfailed", failed)

    entry = har_of(recorder.take("profile"))["log"]["entries"][0]
    assert entry["response"]["status"] == 0
    assert entry["response"]["_failure"] == "net::ERR"


# -- redaction ---------------------------------------------------------


def test_credential_headers_are_fingerprinted_not_stored(recorder, context):
    secret = "AQEDATest_li_at_value_that_must_never_be_stored"
    request = FakeRequest("https://www.linkedin.com/in/x/",
                          headers={"cookie": f"li_at={secret}",
                                   "user-agent": "Chrome"})
    exchange(context, request,
             FakeResponse(request, headers={"set-cookie": f"JSESSIONID={secret}"}))

    blob = recorder.take("profile")
    assert secret.encode() not in gzip.decompress(blob)

    entry = har_of(blob)["log"]["entries"][0]
    sent = {header["name"]: header["value"] for header in entry["request"]["headers"]}
    assert sent["cookie"].startswith("sha256:")
    # Header *names* carry the signal and must survive.
    assert "user-agent" in sent and sent["user-agent"] == "Chrome"
    back = {header["name"]: header["value"]
            for header in entry["response"]["headers"]}
    assert back["set-cookie"].startswith("sha256:")


def test_a_token_in_the_query_string_is_redacted_everywhere(recorder, context):
    token = "SAPISIDHASH_1234567890abcdef"
    request = FakeRequest(f"https://www.google.com/log?authuser=0&auth={token}",
                          page_url="https://www.google.com/search")
    exchange(context, request, FakeResponse(request))

    blob = recorder.take("discovery-serp")
    assert token.encode() not in gzip.decompress(blob)

    entry = har_of(blob)["log"]["entries"][0]
    assert token not in entry["request"]["url"]
    query = {item["name"]: item["value"] for item in entry["request"]["queryString"]}
    assert query["auth"].startswith("sha256:")
    # A parameter that is not a credential is left alone.
    assert query["authuser"] == "0"


# -- body policy -------------------------------------------------------


def test_a_small_first_party_json_body_is_kept(recorder, context):
    payload = b'{"profile": "data"}'
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    exchange(context, request, FakeResponse(
        request, headers=json_headers(len(payload)), body=payload))

    content = har_of(recorder.take("profile"))["log"]["entries"][0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"]) == payload


def test_a_document_body_is_not_kept_because_html_already_is(recorder, context):
    request = FakeRequest("https://www.linkedin.com/in/x/",
                          resource_type="document")
    response = FakeResponse(request, headers={"content-type": "text/html",
                                              "content-length": "10"},
                            body=b"<html></h>")
    exchange(context, request, response)

    content = har_of(recorder.take("profile"))["log"]["entries"][0]["response"]["content"]
    assert "text" not in content
    # Never even asked: the round trip is the expensive part.
    assert response.body_calls == 0


def test_an_asset_host_body_is_not_kept(recorder, context):
    request = FakeRequest("https://static.licdn.com/aero-v1/sc/h/bundle")
    response = FakeResponse(request, headers=json_headers(100), body=b"x" * 100)
    exchange(context, request, response)

    content = har_of(recorder.take("profile"))["log"]["entries"][0]["response"]["content"]
    assert "text" not in content
    assert response.body_calls == 0


def test_a_third_party_body_is_not_kept(recorder, context):
    """A tracker's payload is not evidence about the page we captured."""
    request = FakeRequest("https://collector.protechts.net/beacon",
                          page_url="https://www.linkedin.com/in/x/")
    response = FakeResponse(request, headers=json_headers(50), body=b"y" * 50)
    exchange(context, request, response)

    content = har_of(recorder.take("profile"))["log"]["entries"][0]["response"]["content"]
    assert "text" not in content
    assert response.body_calls == 0


def test_first_party_is_judged_against_the_page_not_a_vendor_list(recorder, context):
    """On a Google detour, Google is first-party and is kept."""
    request = FakeRequest("https://www.google.com/async/folsrch",
                          page_url="https://www.google.com/search?q=x")
    payload = b'{"results": 1}'
    exchange(context, request, FakeResponse(
        request, headers=json_headers(len(payload)), body=payload))

    content = har_of(recorder.take("discovery-serp"))["log"]["entries"][0]["response"]["content"]
    assert base64.b64decode(content["text"]) == payload


def test_media_is_skipped_whatever_resource_type_carries_it(recorder, context):
    request = FakeRequest("https://www.linkedin.com/media/segment")
    response = FakeResponse(request, headers={"content-type": "video/mp4",
                                              "content-length": "100"},
                            body=b"z" * 100)
    exchange(context, request, response)

    content = har_of(recorder.take("profile"))["log"]["entries"][0]["response"]["content"]
    assert "text" not in content
    assert response.body_calls == 0


def test_a_body_over_the_declared_cap_is_never_fetched(recorder, context):
    """The cap is enforced from headers, before the expensive round trip."""
    request = FakeRequest("https://www.linkedin.com/flagship-web/rsc-action")
    response = FakeResponse(request,
                            headers=json_headers(BODY_MAX_BYTES + 1),
                            body=b"q" * (BODY_MAX_BYTES + 1))
    exchange(context, request, response)

    entry = har_of(recorder.take("profile"))["log"]["entries"][0]
    assert "text" not in entry["response"]["content"]
    assert response.body_calls == 0
    # The size is still answerable; only the payload is missing.
    assert entry["response"]["content"]["size"] == BODY_MAX_BYTES + 1


def test_a_body_that_inflates_past_the_ceiling_is_dropped_after_fetch(
        recorder, context):
    """content-length is gzipped; response.body() is not."""
    huge = b"w" * (BODY_STORE_MAX_BYTES + 1)
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    response = FakeResponse(request, headers=json_headers(1024), body=huge)
    exchange(context, request, response)

    entry = har_of(recorder.take("profile"))["log"]["entries"][0]
    assert response.body_calls == 1
    assert "text" not in entry["response"]["content"]
    assert entry["response"]["content"]["size"] == len(huge)
    assert recorder.bodies_skipped == 1


def test_a_zero_length_body_is_not_fetched(recorder, context):
    request = FakeRequest("https://www.linkedin.com/ping")
    response = FakeResponse(request, headers=json_headers(0))
    exchange(context, request, response)

    assert response.body_calls == 0


def test_a_body_that_cannot_be_read_is_not_fatal(recorder, context):
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    response = FakeResponse(request, headers=json_headers(10),
                            raises=RuntimeError("connection closed"))
    exchange(context, request, response)

    entry = har_of(recorder.take("profile"))["log"]["entries"][0]
    assert "text" not in entry["response"]["content"]
    assert entry["response"]["status"] == 200


def test_the_capture_body_budget_bounds_memory(recorder, context):
    """A page unlike any measured one still cannot grow this without limit."""
    payload = b"p" * BODY_MAX_BYTES
    for index in range(CAPTURE_BODY_BUDGET // BODY_MAX_BYTES + 3):
        request = FakeRequest(f"https://www.linkedin.com/voyager/api/{index}")
        exchange(context, request, FakeResponse(
            request, headers=json_headers(len(payload)), body=payload))

    entries = har_of(recorder.take("profile"))["log"]["entries"]
    kept = [entry for entry in entries if "text" in entry["response"]["content"]]
    assert len(kept) * BODY_MAX_BYTES <= CAPTURE_BODY_BUDGET
    # Everything is still recorded; only the payloads stop.
    assert len(entries) == CAPTURE_BODY_BUDGET // BODY_MAX_BYTES + 3


def test_bodies_off_records_metadata_only(context):
    recorder = HarRecorder(bodies=False)
    ctx = FakeContext()
    recorder.attach(ctx)
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    response = FakeResponse(request, headers=json_headers(4), body=b"data")
    exchange(ctx, request, response)

    entry = har_of(recorder.take("profile"))["log"]["entries"][0]
    assert response.body_calls == 0
    assert "text" not in entry["response"]["content"]
    assert entry["request"]["headers"]  # metadata is still there


# -- teardown ----------------------------------------------------------


def test_close_stops_recording_and_counts_what_was_in_flight(recorder, context):
    stranded = FakeRequest("https://www.linkedin.com/inflight")
    context.emit("request", stranded)
    recorder.close()

    assert recorder.pending_dropped == 1
    # Handlers are inert afterwards, and teardown flushes events.
    later = FakeRequest("https://www.linkedin.com/after")
    exchange(context, later, FakeResponse(later))
    assert recorder.take("profile") is None


def test_close_is_idempotent(recorder, context):
    recorder.close()
    recorder.close()


# -- host rule ---------------------------------------------------------


@pytest.mark.parametrize("host, expected", [
    ("in.linkedin.com", "linkedin.com"),
    ("www.linkedin.com", "linkedin.com"),
    ("linkedin.com", "linkedin.com"),
    ("www.google.co.in", "google.co.in"),
    ("static.licdn.com", "licdn.com"),
    ("localhost", "localhost"),
])
def test_registrable_domain(host, expected):
    assert registrable(host) == expected


# -- regressions found against live LinkedIn traffic --------------------


def test_linkedins_csrf_token_is_redacted_because_it_is_the_session_id(
        recorder, context):
    """csrf-token carries the JSESSIONID value verbatim.

    Found in a real capture: the cookie header was fingerprinted and this one
    was not, so the HAR still held a live session credential in plaintext.
    """
    session = "ajax:5914799048743441752"
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql",
                          headers={"csrf-token": session,
                                   "cookie": f"JSESSIONID=\"{session}\""})
    exchange(context, request, FakeResponse(request))

    blob = recorder.take("profile")
    assert session.encode() not in gzip.decompress(blob)
    sent = {h["name"]: h["value"]
            for h in har_of(blob)["log"]["entries"][0]["request"]["headers"]}
    assert sent["csrf-token"].startswith("sha256:")


@pytest.mark.parametrize("header, value", [
    ("x-li-application-instance", "f8UXyBWdRfW8W4zKjYMGmw=="),
    ("x-li-pageforestid", "0006598bdd09f22d00a73aad7ca6620b"),
    ("x-li-page-instance", "urn:li:page:d_flagship3_profile_view_base;wzmuk"),
    ("x-li-page-instance-tracking-id", "wzmukYOPR062sKQ4bUMfHw=="),
    ("x-li-realtime-session", "4c67f9dc-4e07-4855-a201-73af0c395a2a"),
    ("x-li-traceparent", "00-0006598bdd09f22d00a73aad7ca6620b-f0579c6b-00"),
    ("x-li-tracestate", "LinkedIn=f0579c6b5a4f07a5"),
])
def test_account_linked_identifiers_are_fingerprinted(recorder, context,
                                                      header, value):
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql",
                          headers={header: value})
    exchange(context, request, FakeResponse(request))

    blob = recorder.take("profile")
    assert value.encode() not in gzip.decompress(blob)


def test_the_device_fingerprint_header_is_kept(recorder, context):
    """x-li-track names no account, and its shape is the signal we want."""
    track = '{"clientVersion":"0.2.6868","osName":"web","displayWidth":1920}'
    request = FakeRequest("https://www.linkedin.com/voyager/api/graphql",
                          headers={"x-li-track": track})
    exchange(context, request, FakeResponse(request))

    sent = {h["name"]: h["value"] for h in
            har_of(recorder.take("profile"))["log"]["entries"][0]["request"]["headers"]}
    assert sent["x-li-track"] == track


def test_extension_probes_are_counted_not_recorded(recorder, context):
    """LinkedIn fired 695 of these on one measured section capture."""
    real = FakeRequest("https://www.linkedin.com/voyager/api/graphql")
    exchange(context, real, FakeResponse(real))
    for _ in range(20):
        probe = FakeRequest("chrome-extension://invalid/",
                            failure="net::ERR_FAILED")
        context.emit("request", probe)
        context.emit("requestfailed", probe)

    log = har_of(recorder.take("profile"))["log"]
    assert len(log["entries"]) == 1
    assert log["pages"][0]["_extensionProbes"] == 20


def test_the_probe_count_resets_with_each_capture(recorder, context):
    for _ in range(3):
        probe = FakeRequest("chrome-extension://invalid/")
        context.emit("request", probe)
        context.emit("requestfailed", probe)
    first = FakeRequest("https://www.linkedin.com/a")
    exchange(context, first, FakeResponse(first))
    assert har_of(recorder.take("profile"))["log"]["pages"][0]["_extensionProbes"] == 3

    second = FakeRequest("https://www.linkedin.com/b")
    exchange(context, second, FakeResponse(second))
    assert har_of(recorder.take("skills"))["log"]["pages"][0]["_extensionProbes"] == 0
