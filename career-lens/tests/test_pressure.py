"""Interventions: the vocabulary, the recorder and the two emission sites."""

import threading

import pytest

from career_lens.capture import detect, pressure
from career_lens.journal import Journal


@pytest.fixture
def journal():
    """A journal that buffers but never writes.

    `Journal.__init__` starts a flushing thread that would need a database;
    this exercises the buffering and deduplication on their own, which is
    where the logic under test lives.
    """
    one = Journal.__new__(Journal)
    one.attempt_id = 1
    one.url = None
    one._started = 0.0
    one._pending = []
    one._interventions = []
    one._noted = set()
    one._lock = threading.Lock()
    pressure.activate(one.intervention)
    yield one
    pressure.close()


@pytest.fixture
def recorded():
    """Collect interventions instead of writing them."""
    seen = []
    pressure.activate(lambda **fields: seen.append(fields))
    yield seen
    pressure.close()


def test_targeted_is_a_subset_of_every_kind_we_know():
    """Otherwise a kind could sort first in the tooltip and never be recorded."""
    assert pressure.TARGETED <= pressure.INTERVENTIONS


def test_the_check_constraint_vocabulary_is_ordered():
    """A frozenset's order is not stable, and the CHECK is generated from it."""
    assert pressure.INTERVENTION_KINDS == tuple(sorted(pressure.INTERVENTIONS))


def test_recording_without_an_active_recorder_is_a_no_op():
    pressure.close()
    pressure.record(kind="captcha", outcome="blocked", host="google.com")


def test_a_blocking_challenge_is_recorded_as_blocked(recorded):
    challenge = detect.Challenge(
        kind="captcha", evidence="recaptcha iframe",
        url="https://www.google.com/sorry/index")

    assert detect.is_blocking(challenge) is True
    assert recorded == [{
        "kind": "captcha", "outcome": "blocked", "host": "google.com",
        "evidence": "recaptcha iframe",
    }]


def test_a_consent_interstitial_is_classified_but_not_recorded(recorded):
    """Routine: every visitor meets one, so it says nothing about us.

    Still classified -- the flow needs to know it is not blocking -- but it
    stays in the journal rather than putting a warning on the row.
    """
    challenge = detect.Challenge(
        kind="consent", evidence="we use cookies",
        url="https://consent.google.com/m")

    assert detect.is_blocking(challenge) is False
    assert recorded == []


def test_no_challenge_records_nothing(recorded):
    assert detect.is_blocking(None) is False
    assert recorded == []


def test_an_unknown_kind_is_dropped_rather_than_written(recorded):
    """The database CHECK would reject it; refusing here keeps it one warning."""
    pressure.record(kind="not_a_real_kind", outcome="blocked", host="x.com")

    assert recorded == []


def test_an_unknown_outcome_is_dropped(recorded):
    pressure.record(kind="captcha", outcome="sidestepped", host="x.com")

    assert recorded == []


def test_a_recorder_that_raises_cannot_change_the_predicate():
    """The whole reason a predicate may write: its answer must not depend on it."""
    def explode(**fields):
        raise RuntimeError("the journal is gone")

    pressure.activate(explode)
    try:
        challenge = detect.Challenge(
            kind="cloudflare", evidence="just a moment",
            url="https://example.com/")
        assert detect.is_blocking(challenge) is True
    finally:
        pressure.close()


def test_the_host_comes_from_the_challenge_not_the_request(recorded):
    """One request touches many hosts; the wall belongs to the one that raised it."""
    challenge = detect.Challenge(
        kind="cloudflare", evidence="just a moment",
        url="https://www.ft.com/content/abc")

    detect.is_blocking(challenge)

    assert recorded[0]["host"] == "ft.com"


def test_the_same_challenge_asked_about_twice_is_noted_once(journal):
    """`is_blocking` is a predicate several code paths ask independently.

    linkedin/company.py asks three times about one flow's challenge and
    referral.py twice in succession. Without deduplication the count would
    report how many code paths cared, not what the page did.
    """
    challenge = detect.Challenge(
        kind="linkedin_authwall", evidence="sign in to see",
        url="https://www.linkedin.com/authwall")

    for _ in range(3):
        detect.is_blocking(challenge)

    assert len(journal._interventions) == 1
    assert journal._interventions[0].kind == "linkedin_authwall"


def test_the_same_kind_on_two_hosts_stays_two_records(journal):
    """Deduplication must not collapse genuinely different news."""
    for host in ("https://www.google.com/x", "https://www.linkedin.com/y"):
        detect.is_blocking(detect.Challenge(
            kind="captcha", evidence="i\'m not a robot", url=host))

    assert [one.host for one in journal._interventions] == [
        "google.com", "linkedin.com"]


def test_an_overlay_dismissal_and_a_challenge_are_both_recorded(journal):
    """Two emission sites, one stream, distinguished by outcome.

    The overlay here is a LinkedIn sign-in modal rather than a cookie banner:
    only targeted kinds reach the row.
    """
    pressure.record(kind="linkedin_signin_modal", outcome="dismissed",
                    host="linkedin.com", evidence="main frame")
    detect.is_blocking(detect.Challenge(
        kind="captcha", evidence="press and hold",
        url="https://example.com/gate"))

    assert [(one.kind, one.outcome) for one in journal._interventions] == [
        ("linkedin_signin_modal", "dismissed"), ("captcha", "blocked")]


def test_routine_kinds_are_dropped_in_both_runtimes(journal):
    """The gate is `pressure.record`, which every emission site goes through.

    Native dismissal, native challenge, node dismissal and node challenge all
    call it, so one rule covers both runtimes and cannot drift apart.
    """
    for kind in sorted(pressure.INTERVENTIONS - pressure.TARGETED):
        pressure.record(kind=kind, outcome="dismissed", host="example.com")

    assert journal._interventions == []


def test_every_targeted_kind_is_recorded(journal):
    """The other half of the rule, so a typo in TARGETED cannot silence one."""
    for kind in sorted(pressure.TARGETED):
        pressure.record(kind=kind, outcome="blocked", host="example.com")

    assert {one.kind for one in journal._interventions} == set(pressure.TARGETED)


def _console_vocabulary() -> tuple[set[str], set[str]]:
    """The two sets the console keeps, read out of status.tsx."""
    import pathlib
    import re

    source = pathlib.Path("frontend/src/ui/status.tsx").read_text()
    labels = source[source.index("INTERVENTION_LABELS"):
                    source.index("/* Aimed at us")]
    targeted = source[source.index("export const TARGETED"):
                      source.index("export function interventionLabel")]
    return (set(re.findall(r"^  ([a-z_]+):", labels, re.M)),
            set(re.findall(r'"([a-z_]+)"', targeted)))


def test_two_threads_do_not_see_each_other_s_recorder():
    """The reason this is a ContextVar and `trace`'s sink is not.

    A global would file one thread's CAPTCHA against another thread's attempt.
    """
    import threading

    seen: dict[str, list] = {"a": [], "b": []}
    started = threading.Barrier(2)

    def run(name: str) -> None:
        pressure.activate(lambda **fields: seen[name].append(fields["host"]))
        started.wait(timeout=5)
        pressure.record(kind="captcha", outcome="blocked", host=name)

    threads = [threading.Thread(target=run, args=(name,))
               for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert seen == {"a": ["a"], "b": ["b"]}


def test_closing_with_a_token_restores_what_was_there_before():
    """Nesting: an inner scope must not silently disarm the outer one."""
    outer, inner = [], []
    pressure.activate(lambda **f: outer.append(f["host"]))
    try:
        token = pressure.activate(lambda **f: inner.append(f["host"]))
        pressure.record(kind="captcha", outcome="blocked", host="inner.com")
        pressure.close(token)
        pressure.record(kind="captcha", outcome="blocked", host="outer.com")
    finally:
        pressure.close()

    assert inner == ["inner.com"]
    assert outer == ["outer.com"]


