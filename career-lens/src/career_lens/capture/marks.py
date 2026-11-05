"""Attributes this codebase puts on a live page, and takes off again.

Kept apart from `page.py` on purpose. The API image installs neither lxml nor
the rest of the capture group -- it has no reason to carry a browser stack --
and `api.fleet` reaches `capture.browser` for one constant. Anything that
module can transitively import has to stay importable without those
dependencies, so a shared name lives here rather than beside the code that
uses lxml to act on it.
"""

from __future__ import annotations

#: Marks a node as ours rather than the site's. `page._stored_html` drops
#: every element carrying it before any HTML is written, so a debugging aid
#: cannot end up recorded as though it had been served.
ARTIFACT_ONLY = "data-career-lens-artifact-only"
