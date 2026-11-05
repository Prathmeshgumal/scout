"""Browser-independent Google destination selection and slot accounting."""

from career_lens.google.results_contract import SearchResult
from career_lens.google.selection_contract import (
    apply_resolution,
    classify_destination,
    consumes_slot,
    decline_reason,
    linkedin_url_on_host,
    plan_selection,
)


def result(rank, url, host):
    return SearchResult(
        rank=rank, url=url, normalised_url=url, host=host,
        title="t", snippet="s")


def test_classifies_linkedin_destinations_as_shallow():
    assert classify_destination(
        "https://www.linkedin.com/in/example/") == "linkedin_shallow"
    assert classify_destination(
        "https://www.linkedin.com/company/stripe/") == "linkedin_shallow"
    assert classify_destination("https://a.example/one") == "generic"


def test_linkedin_without_a_session_is_declined():
    url = "https://www.linkedin.com/in/example/"
    assert decline_reason(url, linkedin_available=False) == "linkedin_unavailable"
    assert decline_reason(url, linkedin_available=True) is None


def test_an_excluded_host_consumes_its_slot_and_linkedin_does_not():
    assert consumes_slot("excluded_host") is True
    assert consumes_slot("linkedin_unavailable") is False


def test_selection_stops_at_top_n_depth():
    results = [result(index + 1, f"https://a{index}.example/x", f"a{index}.example")
               for index in range(4)]
    observations = plan_selection(results, top_n=2, linkedin_available=True)
    assert [item.selection_reason for item in observations] == [
        "top_n", "top_n", "beyond_top_n", "beyond_top_n"]
    assert [item.selected for item in observations] == [True, True, False, False]


def test_top_n_zero_selects_nothing():
    results = [result(1, "https://a.example/one", "a.example")]
    observations = plan_selection(results, top_n=0, linkedin_available=True)
    assert observations[0].selected is False
    assert observations[0].selection_reason == "beyond_top_n"


def test_a_slot_given_up_passes_to_the_next_taker():
    results = [
        result(1, "https://www.linkedin.com/in/one/", "www.linkedin.com"),
        result(2, "https://a.example/two", "a.example"),
    ]
    observations = plan_selection(results, top_n=1, linkedin_available=False)
    assert observations[0].selection_reason == "linkedin_unavailable"
    assert observations[0].selected is False
    assert observations[1].selection_reason == "top_n"
    assert observations[1].selected is True


def test_resolution_after_a_click_can_decline_and_hand_the_slot_on():
    results = [
        result(1, "https://www.google.com/goto?url=opaque", "www.google.com"),
        result(2, "https://a.example/two", "a.example"),
    ]
    observations = plan_selection(results, top_n=1, linkedin_available=False)
    assert observations[0].selected is True
    resolved = result(1, "https://www.linkedin.com/in/one/", "www.linkedin.com")
    updated = apply_resolution(
        observations, 0, resolved, linkedin_available=False)
    assert updated[0].selection_reason == "linkedin_unavailable"
    assert updated[0].selected is False
    assert updated[1].selection_reason == "top_n"
    assert updated[1].selected is True


def test_a_resolution_that_consumes_its_slot_hands_nothing_on():
    results = [
        result(1, "https://www.google.com/goto?url=opaque", "www.google.com"),
        result(2, "https://a.example/two", "a.example"),
    ]
    observations = plan_selection(results, top_n=1, linkedin_available=True)
    resolved = result(1, "https://www.instagram.com/x/", "www.instagram.com")
    updated = apply_resolution(
        observations, 0, resolved, linkedin_available=True)
    assert updated[0].selection_reason == "excluded_host"
    assert updated[1].selection_reason == "beyond_top_n"
    assert updated[1].selected is False


def test_a_resolution_that_stays_openable_keeps_its_selection():
    results = [result(1, "https://www.google.com/goto?url=opaque", "www.google.com")]
    observations = plan_selection(results, top_n=1, linkedin_available=True)
    resolved = result(1, "https://a.example/one", "a.example")
    updated = apply_resolution(
        observations, 0, resolved, linkedin_available=True)
    assert updated[0].selected is True
    assert updated[0].selection_reason == "top_n"
    assert updated[0].search_result.normalised_url == "https://a.example/one"


def test_a_linkedin_route_moves_to_the_verified_session_host():
    assert linkedin_url_on_host(
        "https://in.linkedin.com/company/stripe", "www.linkedin.com"
    ) == "https://www.linkedin.com/company/stripe"
