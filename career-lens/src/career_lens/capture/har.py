"""Every request the browser made, and what came back, as one HAR per capture.

Whether we read as a browser or a script is settled largely by the headers,
before any page code runs, so the whole exchange is recorded rather than just
the navigation. Responses matter as much as requests: a refusal usually
announces itself on the way back, in a status or a `set-cookie`, not in what we
sent.

The output is standard HAR 1.2, gzipped, stored as one `har` artifact beside
the page's HTML. Standard because the point of choosing a blob over a table was
that DevTools and every HAR viewer already read it.

Attachment is `context.on(...)` only. This module must never call `route`,
`add_init_script`, `expose_function` or `new_cdp_session`: the first three
break Patchright's stealth outright, and the fourth puts the browser on a
protocol surface it otherwise leaves alone. Passive listeners do not touch the
page. The cost is header fidelity -- Chromium withholds `referer` and
`sec-fetch-*` from `request.headers`, because the network service adds them
after the renderer hands off -- and that cost is accepted deliberately.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..redact import redact_header, redact_url

HAR_VERSION = "1.2"
CREATOR = "career_lens"

# Bodies are kept for these resource types only. A `document` body is the page
# itself, which is already stored as an `html` artifact -- keeping it again
# duplicates roughly half a gigabyte across a thousand profiles.
BODY_TYPES = frozenset({"xhr", "fetch"})

# Checked against the declared `content-length`, which is the compressed size.
# 512KB rather than a rounder number because it sits on a plateau: on a profile
# page nothing at all lies between 512KB and 1MB, and the next payloads up are
# the ~4.9MB `rsc-action/actions/component` responses. Being slightly wrong
# about the threshold therefore costs nothing.
BODY_MAX_BYTES = 512 * 1024

# Re-checked after fetching, because `response.body()` returns *decompressed*
# bytes while the cap above reads a gzipped `content-length`. Measured
# inflation on a real run was ~2x, with a worst case of 3.1x, so a body that
# arrives above this ratio is pathological and dropped rather than stored.
BODY_STORE_MAX_BYTES = 4 * BODY_MAX_BYTES

# Total body bytes one capture may hold in memory. Reached only by a page that
# behaves nothing like the measured ones -- a profile capture holds well under
# a megabyte -- and bounded because eight workers share a host.
CAPTURE_BODY_BUDGET = 16 * 1024 * 1024

# Requests still in flight. Only a page that never settles approaches this, and
# eviction writes the entry rather than dropping it.
PENDING_LIMIT = 512

# Chromium answers a page's probe for an absent extension resource with a
# request to this scheme, which always fails as net::ERR_FAILED. LinkedIn
# fires hundreds per section page -- 695 of 800 entries on one measured
# capture -- and none of them is a request the page actually made over the
# network. They are counted rather than recorded: the *number* is the signal
# that a site is probing for extensions, and the entries are pure bloat.
PROBE_SCHEME = "chrome-extension://"

# Media, whatever resource type carries it. A feed fetches video segments and
# their manifests over xhr, so resource type alone does not separate them from
# the API replies worth keeping.
SKIP_CONTENT = ("video/", "audio/", "image/", "font/", "application/dash+xml",
                "application/x-mpegurl", "application/vnd.yt-ump", "text/vtt")

# Asset hosts, whose payloads are identical across every capture and say
# nothing about the page. `static.licdn.com`'s bundles alone run 1.4MB, 765KB
# and 503KB on a single profile.
ASSET_HOSTS = frozenset({
    "static.licdn.com", "dms.licdn.com", "media.licdn.com",
    "www.gstatic.com", "ssl.gstatic.com", "fonts.gstatic.com",
    "fonts.googleapis.com", "apis.google.com",
})

# Enough of the public suffix list to get the hosts this tool visits right.
# A full PSL dependency buys nothing here: the rule only has to decide whether
# a subresource is first-party to the page being captured.
MULTI_PART_TLDS = frozenset({
    "co.uk", "co.in", "co.jp", "co.kr", "co.nz", "co.za", "com.au", "com.br",
    "com.cn", "com.mx", "com.sg", "com.tr", "ne.jp", "or.jp",
})


def registrable(host: str) -> str:
    """The domain a host belongs to, for deciding first-party-ness."""
    labels = (host or "").lower().strip(".").split(".")
    if len(labels) < 2:
        return ".".join(labels)
    if len(labels) >= 3 and ".".join(labels[-2:]) in MULTI_PART_TLDS:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _header_list(values: Any, *, redact: bool) -> list[dict[str, str]]:
    """HAR's [{name, value}] form, redacted.

    Header *names* are never redacted: which headers a client sends, and in
    what order, is the signal this file exists to record.
    """
    out = []
    for name, value in dict(values or {}).items():
        name, value = str(name), str(value)
        out.append({"name": name,
                    "value": redact_header(name, value, enabled=redact)})
    return out


def _content_type(headers: dict[str, str]) -> str:
    for name, value in (headers or {}).items():
        if str(name).lower() == "content-type":
            return str(value).split(";")[0].strip().lower()
    return ""


def _declared_length(headers: dict[str, str]) -> int | None:
    for name, value in (headers or {}).items():
        if str(name).lower() == "content-length":
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    return None


def _query_list(url: str) -> list[dict[str, str]]:
    try:
        return [{"name": name, "value": value}
                for name, value in parse_qsl(urlsplit(url).query,
                                             keep_blank_values=True)]
    except ValueError:
        return []


def _timings(request: Any) -> tuple[datetime, float]:
    """(started_at, total_ms), the browser's own clock preferred.

    Not simply the time the handler ran. Playwright dispatches events only when
    the loop is pumped, and a capture spends much of its time asleep between
    actions, so events queue during a sleep and then fire in a burst: one entry
    would absorb the whole wait and the next would read milliseconds after it.
    """
    try:
        timing = request.timing or {}
        start = timing.get("startTime") or 0
        if start > 0:
            end = timing.get("responseEnd") or 0
            return (datetime.fromtimestamp(start / 1000, tz=UTC),
                    round(end, 1) if end > 0 else -1)
    except Exception:
        pass
    return _now(), -1


class HarRecorder:
    """Accumulates entries, and hands them over one capture at a time.

    `take()` drains everything that has settled since the last call. Because
    the driver captures pages strictly in sequence, that is exactly the traffic
    belonging to the page it is about to yield -- no per-request labelling
    needed. A subresource that started on the previous page and settles after
    the take lands in the next capture's file, late but accurate; recording it
    early as a failure would be worse than recording it late as what it was.
    """

    def __init__(self, *, bodies: bool = True, redact: bool = True,
                 cap_bytes: int = BODY_MAX_BYTES):
        self.bodies = bodies
        self.redact = redact
        self.cap_bytes = cap_bytes
        self.entries_written = 0
        self.bodies_written = 0
        self.bodies_skipped = 0
        self.pending_dropped = 0
        self.probes = 0
        self._entries: list[dict[str, Any]] = []
        self._pending: OrderedDict[Any, None] = OrderedDict()
        self._responses: dict[Any, Any] = {}
        self._body_bytes = 0
        self._context: Any = None
        self._closed = False

    # -- lifecycle -----------------------------------------------------

    def attach(self, context: Any) -> None:
        self._context = context
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_settled)
        context.on("requestfailed", self._on_settled)

    def close(self) -> None:
        """Stop listening. Requests still in flight are counted, not invented."""
        if self._closed:
            return
        if self._context is not None:
            for event, handler in (("request", self._on_request),
                                   ("response", self._on_response),
                                   ("requestfinished", self._on_settled),
                                   ("requestfailed", self._on_settled)):
                try:
                    self._context.remove_listener(event, handler)
                except Exception:
                    # Not fatal: `_closed` below makes the handlers no-ops.
                    pass
        self._closed = True
        self.pending_dropped += len(self._pending)
        self._pending.clear()
        self._responses.clear()

    # -- handlers ------------------------------------------------------

    def _on_request(self, request: Any) -> None:
        if self._closed:
            return
        self._pending[request] = None
        while len(self._pending) > PENDING_LIMIT:
            stale, _ = self._pending.popitem(last=False)
            self._record(stale, settled=False)

    def _on_response(self, response: Any) -> None:
        """Keep the Response the event handed us, resolved and free to read."""
        if self._closed:
            return
        try:
            self._responses[response.request] = response
        except Exception:
            pass

    def _on_settled(self, request: Any) -> None:
        if self._closed:
            return
        self._pending.pop(request, None)
        self._record(request, settled=True)

    # -- body policy ---------------------------------------------------

    def _wants_body(self, request: Any, resource_type: str,
                    headers: dict[str, str], page_host: str) -> bool:
        """Every decision the headers can settle, settled before asking.

        Asking is the expensive part: `response.body()` is a round trip over
        the browser's one channel, taken on Playwright's event-dispatch thread,
        so everything the capture does next waits behind it. Reading every body
        one feed produced cost 222 round trips on a single company and lost
        three screenshots to it.
        """
        if not self.bodies or resource_type not in BODY_TYPES:
            return False
        if self._body_bytes >= CAPTURE_BODY_BUDGET:
            return False
        host = (urlsplit(request.url).hostname or "").lower()
        if host in ASSET_HOSTS:
            return False
        # First-party only. A tracker's payload is not evidence about the page,
        # and the rule generalises: on a LinkedIn capture it keeps LinkedIn, on
        # a Google detour it keeps Google, on an arbitrary URL it keeps that
        # site.
        if page_host and registrable(host) != registrable(page_host):
            return False
        if any(_content_type(headers).startswith(kind) for kind in SKIP_CONTENT):
            return False
        declared = _declared_length(headers)
        if declared == 0:
            return False
        return not (declared is not None and declared > self.cap_bytes)

    def _body(self, response: Any, request: Any, resource_type: str,
              headers: dict[str, str]) -> tuple[bytes | None, int | None]:
        page_host = ""
        try:
            page_host = (urlsplit(response.url).hostname or "").lower()
        except Exception:
            pass
        frame_host = self._page_host(request) or page_host
        if not self._wants_body(request, resource_type, headers, frame_host):
            return None, _declared_length(headers)
        try:
            body = response.body()
        except BaseException:
            # Redirects, aborted requests and cached responses have none. Also
            # CancelledError, which derives from BaseException and so escapes
            # `except Exception`: at teardown the connection goes while calls
            # are still in flight, and every one of those printed a traceback.
            return None, _declared_length(headers)
        # Re-checked after the fetch: the cap above read a gzipped
        # `content-length` and these are decompressed bytes.
        if len(body) > BODY_STORE_MAX_BYTES:
            self.bodies_skipped += 1
            return None, len(body)
        self.bodies_written += 1
        self._body_bytes += len(body)
        return body, len(body)

    @staticmethod
    def _page_host(request: Any) -> str:
        """The host of the page this request was made from."""
        try:
            frame = request.frame
            return (urlsplit(frame.page.url).hostname or "").lower()
        except Exception:
            return ""

    # -- entry building ------------------------------------------------

    def _record(self, request: Any, *, settled: bool) -> None:
        try:
            request_headers = dict(request.headers)
        except Exception:
            # A request whose renderer went away mid-navigation. Losing one
            # entry is not worth taking the capture down for.
            return
        try:
            resource_type = request.resource_type
        except Exception:
            resource_type = ""

        if (getattr(request, "url", "") or "").startswith(PROBE_SCHEME):
            self.probes += 1
            return

        # Logging endpoints put auth tokens straight into the query string,
        # where header redaction would never see them.
        url = redact_url(request.url, enabled=self.redact)
        started_at, total_ms = _timings(request)
        response = self._responses.pop(request, None)

        entry: dict[str, Any] = {
            "startedDateTime": _iso(started_at),
            "time": total_ms,
            "request": {
                "method": getattr(request, "method", "") or "",
                "url": url,
                "httpVersion": "",
                "cookies": [],
                "headers": _header_list(request_headers, redact=self.redact),
                "queryString": _query_list(url),
                "headersSize": -1,
                "bodySize": -1,
            },
            "cache": {},
            "timings": {"send": -1, "wait": -1, "receive": total_ms},
            "_resourceType": resource_type,
        }
        entry["response"] = self._response_object(
            response, request, resource_type, settled)
        self._entries.append(entry)
        self.entries_written += 1

    def _response_object(self, response: Any, request: Any,
                         resource_type: str, settled: bool) -> dict[str, Any]:
        """What came back, from the cached Response only.

        Deliberately never `request.response()`: that waits for a reply, and
        this also runs for requests evicted while still in flight. Google holds
        long-poll connections open for minutes, and waiting on one hangs the
        capture.
        """
        if response is None:
            try:
                failure = request.failure
            except Exception:
                failure = None
            # HAR represents a request that produced no response as status 0.
            return {"status": 0, "statusText": "", "httpVersion": "",
                    "cookies": [], "headers": [],
                    "content": {"size": 0, "mimeType": ""},
                    "redirectURL": "", "headersSize": -1, "bodySize": -1,
                    "_failure": failure}

        try:
            raw = dict(response.headers)
        except Exception:
            raw = {}
        content: dict[str, Any] = {"mimeType": _content_type(raw)}
        size = _declared_length(raw)
        # Only once a request has settled is its body complete; asking earlier
        # waits for the rest of it.
        if settled:
            body, measured = self._body(response, request, resource_type, raw)
            size = measured if measured is not None else size
            if body is not None:
                content["text"] = base64.b64encode(body).decode("ascii")
                content["encoding"] = "base64"
        content["size"] = size if size is not None else -1
        return {
            "status": getattr(response, "status", 0) or 0,
            "statusText": getattr(response, "status_text", "") or "",
            "httpVersion": "",
            "cookies": [],
            "headers": _header_list(raw, redact=self.redact),
            "content": content,
            "redirectURL": "",
            "headersSize": -1,
            "bodySize": content["size"],
        }

    # -- output --------------------------------------------------------

    def take(self, section: str, *, url: str | None = None,
             title: str | None = None) -> bytes | None:
        """Gzipped HAR for everything settled since the last call, or None."""
        entries, self._entries = self._entries, []
        probes, self.probes = self.probes, 0
        self._body_bytes = 0
        if not entries:
            return None
        page_id = f"page_{section}"
        for entry in entries:
            entry["pageref"] = page_id
        document = {
            "log": {
                "version": HAR_VERSION,
                "creator": {"name": CREATOR, "version": HAR_VERSION},
                "pages": [{
                    "startedDateTime": entries[0]["startedDateTime"],
                    "id": page_id,
                    "title": title or url or section,
                    "pageTimings": {"onContentLoad": -1, "onLoad": -1},
                    "_section": section,
                    # How many extension probes this page fired. Kept as a
                    # count because the number is the signal, not the entries.
                    "_extensionProbes": probes,
                }],
                "entries": entries,
            }
        }
        return gzip.compress(
            json.dumps(document, separators=(",", ":")).encode("utf-8"), 6)
