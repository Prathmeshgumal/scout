"""Two fields the parser does not emit yet, derived from ones it does.

Both are requested upstream (spec 5.4). The derivation is conditional on
absence, so the day the parser emits them our values come from the parser with
no code change here -- and deleting this module becomes a cleanup rather than a
cutover.
"""

from __future__ import annotations

import pytest

from career_lens.linkedin.derive import company_id, date_range


@pytest.mark.parametrize("raw,start,end,current", [
    ("Oct 2021 - Jul 2024", "2021-10", "2024-07", False),
    ("Jan 2019 - Present", "2019-01", None, True),
    ("2018 - 2020", "2018", "2020", False),
    ("2018 - Present", "2018", None, True),
    ("Sep 2023 - Present · 1 yr 11 mos", "2023-09", None, True),
    ("Mar 2020 - Jun 2020 · 4 mos", "2020-03", "2020-06", False),
])
def test_a_date_range_yields_its_parts(raw, start, end, current):
    assert date_range(raw) == (start, end, current)


@pytest.mark.parametrize("raw", [None, "", "   ", "sometime", "Full-time"])
def test_an_unparseable_range_yields_nothing_rather_than_a_guess(raw):
    # A wrong date is worse than no date: it is indistinguishable from a right
    # one downstream, and the raw string is kept alongside so nothing is lost.
    assert date_range(raw) == (None, None, None)


def test_a_single_date_with_no_separator_is_not_read_as_a_range():
    assert date_range("Oct 2021") == ("2021-10", None, None)


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/company/162479/", "162479"),
    ("/company/162479/", "162479"),
    ("https://www.linkedin.com/company/stripe/", None),
    ("https://www.linkedin.com/school/12345/", "12345"),
    (None, None),
    ("", None),
    ("https://example.com/company/999/", None),
])
def test_company_id_is_taken_only_from_a_linkedin_numeric_path(url, expected):
    # A slug is not an id: a company can change its slug and the numeric id
    # does not move, which is the entire reason to prefer it.
    assert company_id(url) == expected
