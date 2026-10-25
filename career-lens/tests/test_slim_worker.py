"""What the service captures, and which background work it runs."""

from career_lens.capture.urls import canonical, classify
from career_lens.queue.tasks import app
from career_lens.store.schema.core import KINDS


def test_only_kept_request_kinds():
    assert set(KINDS) == {"linkedin_person", "linkedin_company", "generic", "google_search"}


def test_linkedin_job_url_is_a_plain_page():
    assert classify(canonical("https://www.linkedin.com/jobs/view/123/")) == "generic"


def test_formerly_special_route_is_a_plain_page():
    assert classify(canonical("https://example.org/organization/acme")) == "generic"


def test_linkedin_urls_keep_their_kinds():
    assert classify(canonical("https://www.linkedin.com/in/someone/")) == "linkedin_person"
    assert classify(canonical("https://www.linkedin.com/company/acme/")) == "linkedin_company"


def test_background_tasks_are_the_kept_ones():
    names = {task.name for task in app.tasks.values()
             if not task.name.startswith("builtin:")}
    assert names == {
        "dispatch_pending_requests", "materialise_shift_days",
        "scrape_assigned", "worker_operation",
    }
