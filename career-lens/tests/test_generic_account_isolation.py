"""Public website work must not depend on or quarantine a provider account."""

from datetime import UTC, datetime
import random

import pytest
from sqlalchemy import text

from fleet_support import (
    _anonymous_attempt, _page_session, refuse_linkedin_observation,
)
from career_lens.capture.failures import CaptureFailed
from career_lens.fleet.dispatch import dispatch_one
from career_lens.queue import tasks
from career_lens.store.db import transaction


@pytest.mark.parametrize("cause", ["blocked", "login_required", "account_mismatch"])
@pytest.mark.parametrize("legacy", [False, True])
def test_native_generic_failure_leaves_linkedin_untouched(app, app_url, monkeypatch, cause, legacy):
    request, attempt = _anonymous_attempt(app_url, linkedin=True, kind="generic")
    if legacy:
        with transaction(app_url) as connection:
            connection.execute(text("""
                UPDATE scrape_attempt SET worker_id='worker-linkedin',
                    account_id='la-a', assignment_id='as-a' WHERE id=:id
            """), {"id": attempt})

    def fail(*args, **kwargs):
        raise CaptureFailed(cause, "unrelated website refused access")

    monkeypatch.setattr(tasks, "_work", fail)
    tasks.run_assigned(attempt, url=app_url, session_factory=_page_session,
                       observer=refuse_linkedin_observation)
    assert app.execute(text("SELECT state FROM linkedin_account WHERE id='la-a'")).scalar_one() == "enabled"
    assert app.execute(text("SELECT state FROM account_worker_assignment WHERE id='as-a'")).scalar_one() == "ready"
    assert app.execute(text("SELECT cause FROM scrape_attempt WHERE id=:id"), {"id": attempt}).scalar_one() == cause
    assert app.execute(text("SELECT count(*) FROM linkedin_account_event")).scalar_one() == 0


def test_generic_dispatch_needs_no_linkedin_account(app, app_url):
    request, attempt = _anonymous_attempt(app_url, kind="generic")
    with transaction(app_url) as connection:
        connection.execute(text("UPDATE scrape_attempt SET ended_at=now() WHERE id=:id"), {"id": attempt})
        connection.execute(text("UPDATE scrape_request SET status='queued' WHERE id=:id"), {"id": request})
        assigned = dispatch_one(connection, now=datetime.now(UTC), rng=random.Random(0))
        assert assigned is not None
        row = connection.execute(text("SELECT account_id, assignment_id FROM scrape_attempt WHERE id=:id"), {"id": assigned}).one()
        assert row.account_id is None
        assert row.assignment_id is None
