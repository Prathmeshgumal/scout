"""Turn captured HTML into Markdown.

`inner_text("body")` is what a page looks like with every tag removed: nav,
cookie text, footers and prose run together as one flat stream with no headings
and no links. It is what `content` held before this module existed, and it is
poor material both for reading and for full-text search.

Docling's HTML backend reads the same HTML and produces real structure. This
runs at capture time rather than as a separate pass, so the database holds
Markdown from the moment a page is stored. Using the HTML backend directly is
important: Docling's general converter eagerly imports its PDF, OCR, and model
pipelines even though career-lens never sends it anything but HTML.

`PARSER` is recorded on every row. When this module improves, pages already
captured can be re-derived from their stored HTML rather than only new ones
benefiting -- v1 has no re-derivation command yet, but `capture.markdown_parser`
is what would let one be added without a backfill migration.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

# Bumped whenever conversion changes in a way that would produce different
# output for the same input. `career_lens parse` re-derives anything older.
#
# docling-2 unwraps div-grouped definition lists (below). Pages captured under
# docling-1 are missing whatever those lists held, so re-deriving them is not
# cosmetic. docling-3 reveals a post's engagement counts, which docling-2
# dropped entirely. docling-4 resolves root-relative links against the page
# they came from, so a stored link names the site rather than whatever origin
# happens to render the Markdown.
PARSER = "docling-4"

TEXT = "text"
MARKDOWN = "markdown"

# docling emits the page title as h1. A report puts its own h1 at the top, so
# everything shifts down one level to keep the document coherent.
HEADING = re.compile(r"^(#{1,5})(?= )", re.MULTILINE)


def demote_headings(markdown: str) -> str:
    return HEADING.sub(lambda m: "#" + m.group(1), markdown)


# `href` only, and only the value. `src` is left alone: it points at media the
# capture does not store, so absolutising it would produce a confident link to
# something nobody kept.
HREF = re.compile(rb"""(\bhref\s*=\s*)(["']?)([^"'\s>]+)(\2)""", re.I)

#: Schemes that are not a path on a site, and a fragment, which names a place
#: on this page. None of these is resolved against anything.
OPAQUE = (b"#", b"//", b"mailto:", b"tel:", b"javascript:", b"data:",
          b"blob:", b"sms:", b"ftp:")


def absolutise_links(html: bytes, base_url: str | None) -> bytes:
    """Resolve root-relative `href`s against the page they were captured from.

    A browser reads `/person/x` against the site it is on. Stored Markdown has
    no such context -- the operator console renders it, so the same link points
    at the console and goes nowhere. The capture knows the URL it landed on,
    which is the only correct thing to resolve against.

    Anything already absolute, protocol-relative, a fragment, or a non-http
    scheme is returned untouched. A base we cannot parse leaves the document
    exactly as it arrived: a relative link is a worse link, but a wrongly
    rewritten one is a false claim about where the content came from.
    """
    if not html or not base_url:
        return html
    try:
        parts = urlsplit(base_url)
    except ValueError:
        return html
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return html
    origin = f"{parts.scheme}://{parts.netloc}".encode()

    def resolve(match: re.Match[bytes]) -> bytes:
        value = match.group(3)
        lowered = value.lower()
        if not value.startswith(b"/") or lowered.startswith(OPAQUE):
            return match.group(0)
        return b"%s%s%s%s%s" % (
            match.group(1), match.group(2), origin, value, match.group(4))

    return HREF.sub(resolve, html)


DEFINITION_LIST = re.compile(rb"<dl\b[^>]*>.*?</dl\s*>", re.I | re.S)
DL_WRAPPER = re.compile(rb"</?div\b[^>]*>", re.I)


def unwrap_definition_lists(html: bytes) -> bytes:
    """Drop `<div>` wrappers sitting between a `<dl>` and its `<dt>`/`<dd>`.

    HTML5 allows a `<dl>` to group each term and description in a `<div>`, and
    styling frameworks emit exactly that for spacing. docling reads only the
    direct children of a `<dl>`, so a wrapped list converts to nothing at all
    and the section vanishes with no error.

    This lives here rather than in report.py because this is where content is
    parsed -- at capture time, into the column the UI reads. Having it only in
    the report meant the exported Markdown was right and the stored content,
    which is what everything else reads, silently was not.
    """
    return DEFINITION_LIST.sub(lambda m: DL_WRAPPER.sub(b"", m.group(0)), html)


# The class is on the same tag as the label in all three cases, whatever order
# the attributes happen to be in. No word boundary after it: the classes that
# carry it continue into `__item`, `__btn`, `__reactions-count`, and `_` is a
# word character, so a boundary there matches nothing.
SOCIAL_COUNT_TAG = re.compile(
    rb"<(?:button|a|span)\b[^>]*social-details-social-counts[^>]*>", re.I)
SOCIAL_COUNT_LABEL = re.compile(
    rb'aria-label="\s*([0-9][0-9,]*\s+(?:reaction|comment|repost)s?)\b', re.I)


def reveal_social_counts(html: bytes) -> bytes:
    """Write a post's engagement counts where a reader can see them.

    LinkedIn puts the number in a span marked `aria-hidden` and the wording it
    belongs to in the button's `aria-label`. docling drops the first and does
    not emit the second, so both routes to the number are closed and a post
    runs from its own text straight to "Comment Repost Send".

    The count alone rather than the whole label, which trails off into whose
    post it was and repeats what the entry above it already says.
    """
    def reveal(match: re.Match[bytes]) -> bytes:
        tag = match.group(0)
        label = SOCIAL_COUNT_LABEL.search(tag)
        # The inner span carries the class and no label; it is what was hidden,
        # not what names it.
        return tag if label is None else b"%s %s " % (tag, label.group(1))

    return SOCIAL_COUNT_TAG.sub(reveal, html)


def to_markdown(html: bytes | str, base_url: str | None = None) -> str | None:
    """Convert captured HTML to Markdown. None if docling cannot read it.

    `base_url` is the URL this page was captured from. Root-relative links are
    resolved against it, because stored Markdown is read somewhere the page's
    own origin no longer applies. Omitting it converts as before.

    Never raises: a page we cannot convert still has its HTML in the blob store
    and its row in the database, and losing either to a parser error would be a
    far worse outcome than an unparsed page.
    """
    if not html:
        return None
    if isinstance(html, str):
        html = html.encode("utf-8")

    # docling wants a path, and the caller has bytes in memory because nothing
    # in the capture path touches the filesystem any more.
    handle = tempfile.NamedTemporaryFile("wb", suffix=".html", delete=False)
    try:
        with handle:
            handle.write(absolutise_links(
                reveal_social_counts(unwrap_definition_lists(html)), base_url))
        # Imported here so unrelated `career_lens` commands do not pay even the
        # slim parser's import cost. DocumentConverter cannot be used here: it
        # eagerly imports every document pipeline, including the ML-backed PDF
        # ones, before it knows this input is HTML.
        from docling.backend.html_backend import HTMLDocumentBackend
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.document import InputDocument

        source = Path(handle.name)
        input_document = InputDocument(
            source,
            InputFormat.HTML,
            HTMLDocumentBackend,
        )
        document = input_document._backend.convert()
        markdown = document.export_to_markdown()
        return demote_headings(markdown) if markdown else None
    except Exception:
        return None
    finally:
        Path(handle.name).unlink(missing_ok=True)
