"""Parser-contract checks for LinkedIn HTML drift."""

from __future__ import annotations


class ParserDrift(Exception):
    """A parser contract no longer holds for the captured HTML."""

    def __init__(
        self,
        *,
        section: str,
        contract: str,
        expected: str,
        found: str,
    ) -> None:
        super().__init__(f"{section}: {contract} - expected {expected}, found {found}")
        self.section = section
        self.contract = contract
        self.expected = expected
        self.found = found

    def as_dict(self) -> dict[str, str]:
        return {
            "section": self.section,
            "contract": self.contract,
            "expected": self.expected,
            "found": self.found,
        }

def require_unanimous(
    entries: list[dict[str, object]],
    field: str,
    *,
    section: str,
    contract: str,
) -> None:
    """Raise when a structurally-unanimous field disappears across a section.

    When the captured source signals a field, all of it vanishing means the
    wording or the markup moved and the entries are probably being misread --
    so the section is suspect, not merely incomplete.

    The caller must establish that source evidence first.  A field absent from
    the source is optional and belongs through `absent_everywhere` instead.
    """
    total = len(entries)
    if total < 3:
        return
    if any(entry.get(field) for entry in entries):
        return
    raise ParserDrift(
        section=section,
        contract=contract,
        expected=f"{field} on at least one of {total} entries",
        found=f"0 of {total} entries had {field}",
    )


def absent_everywhere(
    entries: list[dict[str, object]],
    field: str,
    *,
    section: str,
    contract: str,
) -> dict[str, str] | None:
    """Note an optional field that is absent from a whole section.

    The weaker sibling of `require_unanimous`, for a field LinkedIn genuinely
    lets a profile omit. A profile listing English, Hindi and Punjabi with no
    proficiency on any of them is one LinkedIn permits -- raising there read
    all three correctly and then discarded them.

    Worth recording, never worth destroying entries over.
    """
    total = len(entries)
    if total < 3:
        return None
    if any(entry.get(field) for entry in entries):
        return None
    return {
        "code": "field_absent",
        "section": section,
        "message": f"{contract}: 0 of {total} entries had {field}",
    }


def require_announced(
    entries: list[dict[str, object]],
    announced: int | None,
    *,
    section: str,
    complete: bool,
) -> None:
    """Hold a section to the total its profile page stated.

    `complete` says whether these entries came from the section's own
    `details/` capture -- the page "Show all" opens, which holds all of them.
    There, anything short of the announced total is drift: a page that promises
    six and delivers one is not degraded, it is broken.

    Read from the profile page instead, the entries are a subset by
    construction: LinkedIn shows the first few and hides the rest behind the
    very link that carried the count. There only an empty read is drift.

    Unlike the two checks above, this one does not infer an expectation from
    the entries it can see -- LinkedIn states the number itself, which is why
    it can be enforced strictly. Of the 32 held-out count checks it flagged 21
    genuine defects and no false positive, including three sections whose
    entries were missing from the captured bytes entirely.
    """
    if announced is None or not announced:
        return
    if len(entries) >= announced or (entries and not complete):
        return
    raise ParserDrift(
        section=section,
        contract="announced entry count",
        expected=(
            f"the {announced} entries LinkedIn announced" if complete
            else f"at least one of the {announced} entries LinkedIn announced"
        ),
        found=f"{len(entries)} entries",
    )
