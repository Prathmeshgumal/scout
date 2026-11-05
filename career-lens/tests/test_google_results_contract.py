"""Browser-independent Google organic result resolution."""

from career_lens.google.results_contract import ResultRow, ranked_results

SERP = "https://www.google.com/search?q=example"


def row(href, *, click_href=None, title="Example", snippet="A snippet",
        document_context=""):
    return ResultRow(
        href=href,
        click_href=click_href if click_href is not None else href,
        title=title,
        snippet=snippet,
        document_context=document_context,
    )


def test_ranks_unique_results_in_order():
    results = ranked_results(
        [row("https://a.example/one"), row("https://b.example/two")],
        page_url=SERP,
    )
    assert [item.rank for item in results] == [1, 2]
    assert [item.host for item in results] == ["a.example", "b.example"]


def test_drops_duplicate_destinations_without_consuming_a_rank():
    results = ranked_results(
        [row("https://a.example/one"),
         row("https://a.example/one"),
         row("https://b.example/two")],
        page_url=SERP,
    )
    assert [item.normalised_url for item in results] == [
        "https://a.example/one", "https://b.example/two"]
    assert [item.rank for item in results] == [1, 2]


def test_drops_google_internal_links():
    results = ranked_results(
        [row("https://www.google.com/preferences"), row("https://a.example/one")],
        page_url=SERP,
    )
    assert [item.host for item in results] == ["a.example"]


def test_unwraps_a_percent_encoded_goto_redirect():
    results = ranked_results(
        [row("https://www.google.com/goto?url=https%3A%2F%2Fa.example%2Fone",
             click_href="/goto?url=https%3A%2F%2Fa.example%2Fone")],
        page_url=SERP,
    )
    assert results[0].normalised_url == "https://a.example/one"
    assert results[0].opaque_redirect is True
    assert results[0].click_href == "/goto?url=https%3A%2F%2Fa.example%2Fone"


def test_recovers_a_real_opaque_token_from_its_context_window():
    # The form Google actually serves: an opaque id, not a percent-encoded URL.
    # The destination is only recoverable from the document context slice.
    token = "/goto?url=CAESXgHrOzAVRVZjhEEuAERChv9"
    escaped = token.replace("=", "\\u003d")
    opaque = row(
        f"https://www.google.com{token}",
        click_href=token,
        title="Stripe",
        snippet="Stripe LinkedIn 16.8L+ followers",
        document_context=f'"{escaped}"],["https://in.linkedin.com/company/stripe"]',
    )
    results = ranked_results([opaque], page_url=SERP)
    # `url` keeps the address Google offered; `normalised_url` is canonical, so
    # the country host folds to `www` and two spellings of one profile
    # deduplicate. Both are needed: one to open, one to compare.
    assert results[0].url == "https://in.linkedin.com/company/stripe"
    assert results[0].normalised_url == "https://www.linkedin.com/company/stripe/"
    assert results[0].opaque_redirect is True


def test_an_unresolvable_opaque_token_is_kept_as_the_goto_address():
    # Google rotates these tokens between reads, and the recovery path re-reads
    # the page and matches rows by identity. A dropped row would look like a
    # result that vanished, so the goto address stands in for the destination.
    token = "/goto?url=CAESopaque"
    results = ranked_results(
        [row(f"https://www.google.com{token}", click_href=token,
             document_context=""),
         row("https://a.example/one")],
        page_url=SERP,
    )
    assert [item.host for item in results] == ["www.google.com", "a.example"]
    assert results[0].opaque_redirect is True
    assert results[0].click_href == token


def test_a_non_google_link_that_cannot_be_resolved_is_dropped():
    results = ranked_results(
        [row("not-a-url", click_href="not-a-url"), row("https://a.example/one")],
        page_url=SERP,
    )
    assert [item.host for item in results] == ["a.example"]


def test_the_title_prefix_is_stripped_from_the_snippet():
    results = ranked_results(
        [row("https://a.example/one", title="Stripe",
             snippet="Stripe\nLinkedIn - Stripe\n16.8L+ followers")],
        page_url=SERP,
    )
    assert results[0].snippet == "LinkedIn - Stripe 16.8L+ followers"


def test_respects_the_row_limit():
    rows = [row(f"https://a{index}.example/x") for index in range(80)]
    assert len(ranked_results(rows, page_url=SERP, limit=60)) == 60
