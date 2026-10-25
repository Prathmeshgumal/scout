"""Whether a failure gets another attempt. Pure: no database, no queue."""

from __future__ import annotations

from datetime import timedelta

import pytest

from career_lens.fleet import retry
from career_lens.store.schema.core import FAILURE_KINDS

RETRYABLE = {
    "blocked", "login_required", "account_mismatch",
    "worker_lost", "timeout", "browser_error", "storage_error",
}
TERMINAL = {"not_found", "extract_failed", "parser_drift", "cancelled"}


def test_every_known_cause_has_an_explicit_disposition():
    """v2 §4.5: a cause added later may not default to retrying forever.

    The guarantee is kept by exhaustiveness rather than by a column, so this
    test is the guarantee. A tenth cause fails here until someone decides what
    it means -- which is how the Google search work will be forced to.
    """
    for cause in FAILURE_KINDS:
        decision = retry.decide(cause=cause, attempt_no=1, max_attempts=3)
        assert isinstance(decision, (retry.Retry, retry.Terminal)), cause


def test_the_disposition_of_each_cause_is_the_settled_one():
    assert RETRYABLE | TERMINAL == set(FAILURE_KINDS)
    for cause in RETRYABLE:
        assert isinstance(
            retry.decide(cause=cause, attempt_no=1, max_attempts=3),
            retry.Retry), cause
    for cause in TERMINAL:
        assert retry.decide(cause=cause, attempt_no=1, max_attempts=3) == (
            retry.Terminal(retry.CAUSE_IS_TERMINAL)), cause


def test_an_unknown_cause_raises_rather_than_defaulting():
    with pytest.raises(ValueError, match="disposition"):
        retry.decide(cause="sunspots", attempt_no=1, max_attempts=3)


def test_an_unknown_cause_raises_even_at_the_ceiling():
    """The ceiling must not become a place where an undispositioned cause
    passes silently: at the cap every cause returns the same answer, so a cause
    nobody has thought about would be indistinguishable from one that has been
    settled."""
    with pytest.raises(ValueError, match="disposition"):
        retry.decide(cause="sunspots", attempt_no=3, max_attempts=3)


def test_the_cap_ends_a_request_for_scrape_failures():
    for cause in set(FAILURE_KINDS) - retry.INFRASTRUCTURE_CAUSES:
        assert retry.decide(cause=cause, attempt_no=3, max_attempts=3) == (
            retry.Terminal(retry.ATTEMPTS_EXHAUSTED)), cause


def test_the_cap_is_reached_at_equality_not_beyond_it():
    """A cap of three means three attempts, not four."""
    assert isinstance(
        retry.decide(cause="timeout", attempt_no=2, max_attempts=3),
        retry.Retry)
    assert retry.decide(cause="timeout", attempt_no=3, max_attempts=3) == (
        retry.Terminal(retry.ATTEMPTS_EXHAUSTED))


def test_a_cap_of_one_means_no_retries_at_all():
    for cause in set(FAILURE_KINDS) - retry.INFRASTRUCTURE_CAUSES:
        assert retry.decide(cause=cause, attempt_no=1, max_attempts=1) == (
            retry.Terminal(retry.ATTEMPTS_EXHAUSTED)), cause


def test_an_identity_failure_is_ready_immediately():
    """The failed account is already quarantined, so a different one is free
    now and waiting would buy nothing."""
    for cause in ("blocked", "login_required", "account_mismatch"):
        assert retry.decide(
            cause=cause, attempt_no=1, max_attempts=3) == retry.Retry(
                timedelta(0)), cause


def test_a_lost_worker_waits_a_minute():
    assert retry.decide(
        cause="worker_lost", attempt_no=1, max_attempts=3) == retry.Retry(
            timedelta(seconds=60))


def test_infrastructure_loss_bypasses_the_scrape_attempt_ceiling():
    for cause in retry.INFRASTRUCTURE_CAUSES:
        assert isinstance(
            retry.decide(cause=cause, attempt_no=3, max_attempts=3),
            retry.Retry,
        ), cause


def test_a_transient_failure_backs_off_as_it_repeats():
    first = retry.decide(cause="timeout", attempt_no=1, max_attempts=5)
    second = retry.decide(cause="timeout", attempt_no=2, max_attempts=5)
    third = retry.decide(cause="browser_error", attempt_no=3, max_attempts=5)
    assert first == retry.Retry(timedelta(seconds=120))
    assert second == retry.Retry(timedelta(seconds=240))
    assert third == retry.Retry(timedelta(seconds=480))


def test_a_blob_store_failure_is_transient_rather_than_terminal():
    """A store that would not take a verified object says nothing about the
    page or the identity, so the work is worth re-running without spending the
    scrape retry ceiling."""
    assert retry.decide(
        cause="storage_error", attempt_no=1, max_attempts=3) == retry.Retry(
            timedelta(seconds=120))
    assert retry.decide(
        cause="storage_error", attempt_no=3, max_attempts=3) == retry.Retry(
            timedelta(seconds=120))


def test_a_pending_cancel_ends_a_request_whatever_the_cause():
    """The operator's stop outranks the policy's opinion.

    Iterated over the whole vocabulary rather than one retryable cause,
    because the case this exists for is a *retryable* one -- a worker that died
    mid-cancel closes its attempt as `worker_lost`, and without this the
    request would be dispatched again after a human stopped it.
    """
    for cause in FAILURE_KINDS:
        assert retry.decide(cause=cause, attempt_no=1, max_attempts=3,
                            cancel_requested=True) == (
            retry.Terminal(retry.CANCELLED)), cause


def test_a_pending_cancel_outranks_the_exhausted_ceiling():
    """The reason an operator reads should be the one they caused."""
    assert retry.decide(cause="timeout", attempt_no=3, max_attempts=3,
                        cancel_requested=True) == retry.Terminal(
                            retry.CANCELLED)


def test_an_unknown_cause_still_raises_under_a_pending_cancel():
    """Cancelling must not become a second place an undispositioned cause
    passes silently."""
    with pytest.raises(ValueError, match="disposition"):
        retry.decide(cause="sunspots", attempt_no=1, max_attempts=3,
                     cancel_requested=True)


def test_the_terminal_status_of_a_stopping_request():
    assert retry.terminal_status("timeout", True) == "cancelled"
    assert retry.terminal_status("timeout", False) == "failed"
    assert retry.terminal_status(None, False) == "succeeded"


def test_a_run_that_outran_its_cancel_keeps_its_result():
    """The last page landed and the extraction was written before the poll
    could fire: the work is done and the data is good, so recording it as
    cancelled would discard a complete result to honour an instruction the run
    beat."""
    assert retry.terminal_status(None, True) == "succeeded"
