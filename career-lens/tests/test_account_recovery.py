from datetime import UTC, datetime, timedelta

from career_lens.fleet.account_recovery import (
    RecoveryPolicy,
    RecoveryState,
    authenticated,
    configured_policy,
    login_finished,
    signed_out,
)


NOW = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
POLICY = RecoveryPolicy(
    before_relogin=timedelta(minutes=30),
    after_relogin=timedelta(minutes=15),
    failure_limit=2,
)


def test_recovery_cools_before_login_and_after_success():
    scheduled = signed_out(RecoveryState(), now=NOW, policy=POLICY)

    assert scheduled.action == "cooldown"
    assert scheduled.state == RecoveryState(
        phase="before_relogin",
        not_before=NOW + timedelta(minutes=30),
        failures=0,
    )

    due = signed_out(
        scheduled.state,
        now=NOW + timedelta(minutes=30),
        policy=POLICY,
    )
    assert due.action == "login"

    landed = login_finished(
        due.state, landed=True, now=due.state.not_before, policy=POLICY
    )
    assert landed.action == "cooldown"
    assert landed.state == RecoveryState(
        phase="after_relogin",
        not_before=NOW + timedelta(minutes=45),
        failures=0,
    )
    assert (
        authenticated(
            landed.state,
            now=NOW + timedelta(minutes=45),
        ).action
        == "resume"
    )


def test_recovery_stops_after_consecutive_login_failures():
    state = RecoveryState(
        phase="before_relogin",
        not_before=NOW,
        failures=1,
    )

    failed = login_finished(state, landed=False, now=NOW, policy=POLICY)

    assert failed.action == "attention"
    assert failed.state.failures == 2


def test_configured_recovery_varies_once_and_keeps_its_deadline(monkeypatch):
    monkeypatch.setattr(
        "career_lens.fleet.account_recovery.random.uniform",
        lambda _low, high: high,
    )
    policy = configured_policy(30 * 60, 15 * 60)

    scheduled = signed_out(RecoveryState(), now=NOW, policy=policy)
    repeated = signed_out(
        scheduled.state, now=NOW + timedelta(minutes=1), policy=policy,
    )

    assert scheduled.state.not_before == NOW + timedelta(minutes=36)
    assert repeated.state == scheduled.state
