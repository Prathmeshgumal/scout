"""Stable operator-attention reasons and their actionable guidance."""

from __future__ import annotations

import pytest


def test_unobservable_identity_guidance_names_the_page_and_success_condition():
    from career_lens.fleet.attention import guidance

    item = guidance("linkedin_identity_unobservable")

    assert item.title == "LinkedIn identity not observable"
    assert "/in/me/" in item.hint
    assert "assigned account" in item.hint
    assert item.hint.endswith("then Verify.")
    assert item.runbook_anchor == "linkedin-identity-unobservable"


def test_legacy_guidance_leads_with_the_safe_assignment_action():
    from career_lens.fleet.attention import guidance

    hint = guidance("legacy_attention").hint

    assert hint.startswith("Assigned profile: click Verify.")
    assert "Use Clear only to sign out and unassign it." in hint
    assert "If nothing is assigned" in hint


def test_unknown_attention_reason_is_rejected():
    from career_lens.fleet.attention import guidance

    with pytest.raises(ValueError, match="unknown operator attention code"):
        guidance("made_up_reason")


def test_every_guidance_item_has_an_action_and_unique_runbook_anchor():
    from career_lens.fleet.attention import GUIDANCE

    anchors = [item.runbook_anchor for item in GUIDANCE.values()]
    assert len(anchors) == len(set(anchors))
    for code, item in GUIDANCE.items():
        assert item.code == code
        assert item.title.strip()
        assert item.hint.strip()
        assert item.hint.endswith("."), code
        assert item.runbook_anchor.strip()
