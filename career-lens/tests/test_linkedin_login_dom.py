"""Login controls against realistic dynamic markup in a real browser."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.browser


@pytest.fixture
def page(tmp_path):
    from patchright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "profile"), channel="chrome",
            headless=True)
        try:
            yield context.new_page()
        finally:
            context.close()


def test_login_controls_select_only_the_visible_current_form(page, site):
    from career_lens.linkedin.session import login_controls

    page.goto(f"{site}/linkedin-login.html")
    username, password, submit = login_controls(page)

    assert username.count() == 1
    assert password.count() == 1
    assert submit.count() == 1
    assert username.get_attribute("id") == "ember-current-user"
    assert password.get_attribute("id") == "ember-current-password"
    assert submit.get_attribute("id") == "ember-current-submit"
