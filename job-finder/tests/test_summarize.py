from job_finder.summarize import (
    DESCRIPTION_LIMIT,
    MAX_JOBS,
    format_search_results,
    summarize_company,
    summarize_job,
    summarize_job_search,
)

CO = "https://www.linkedin.com/company/acme/"


def test_company_summary_gives_facts_jobs_state_and_roles():
    body = {"entity": {"url": CO}, "observed_at": "2026-09-30",
            "meta": {"jobs": {"status": "parsed", "reached_end": True, "captured_pages": 1}},
            "data": {"profile": {"name": "Acme", "industry": "Software", "employee_count": "201-500 employees"},
                     "about": {"data": {"website": "https://acme.com", "founded": "2015", "overview": "We build anvils."}},
                     "jobs": {"data": {"items": [
                         {"title": "Go Engineer", "location": "Pune", "workplace_type": "Hybrid",
                          "posted_at": "3 weeks ago", "posting_status": "closed", "url": "https://l.in/j/1"},
                         {"title": "SRE"}]}}}}
    text = summarize_company(body)
    for expected in (f"LinkedIn company: {CO}", "Name: Acme", "Industry: Software", "Website: https://acme.com",
                     "Founded: 2015", "Overview: We build anvils.", "no agent read the job text",
                     "- Go Engineer (Pune; Hybrid; posted 3 weeks ago; closed) https://l.in/j/1", "- SRE"):
        assert expected in text, expected


def test_company_summary_caps_the_roles():
    jobs = [{"title": f"R{i}"} for i in range(MAX_JOBS + 5)]
    text = summarize_company({"data": {"jobs": {"data": {"items": jobs}}}})
    assert f"- R{MAX_JOBS - 1}" in text and f"- R{MAX_JOBS}\n" not in text
    assert "(And 5 more roles that this reply does not show.)" in text


def search(reached_end, items, pages=2):
    return {"status": "succeeded", "result": {
        "keywords": "go engineer", "location": "Pune", "observed_at": "2026-11-04T10:00:00Z",
        "captured_pages": pages, "reached_end": reached_end, "items": items}}


def test_job_search_summary_lists_roles_with_company_and_source():
    item = {"title": "Go Engineer", "company_name": "Acme", "location": "Pune", "url": "https://l.in/j/1",
            "insights": ["2 connections work here"]}
    text = summarize_job_search(search(True, [item]))
    for expected in ("LinkedIn job search: go engineer", "Location: Pune", "Read to the end: 1 roles.",
                     "give source linkedin_search", "- Go Engineer (Pune) https://l.in/j/1 — Acme"):
        assert expected in text, expected
    assert "connections" not in text


def test_a_job_search_that_stopped_early_says_partial():
    text = summarize_job_search(search(False, [{"title": "A"}], pages=3))
    assert "PARTIAL: 1 roles from 3 pages. More roles exist." in text
    assert "Read to the end" not in text


def test_an_empty_finished_search_has_no_roles():
    text = summarize_job_search(search(True, []))
    assert "Read to the end: 0 roles." in text and "no agent read" not in text


def job(**fields):
    base = {"job_url": "https://www.linkedin.com/jobs/view/1/", "title": "Go Engineer", "company_name": "Acme",
            "location": "Pune", "posting_status": "open", "description_text": "Build payments in Go."}
    return {"observed_at": "2026-11-04", "job": base | fields}


def test_job_summary_gives_facts_and_full_text():
    text = summarize_job(job(applicants="40 applicants"))
    for expected in ("LinkedIn job: https://www.linkedin.com/jobs/view/1/", "Title: Go Engineer", "Company: Acme",
                     "Applicants: 40 applicants", "Description:\nBuild payments in Go."):
        assert expected in text, expected
    assert "CLOSED" not in text


def test_job_summary_marks_a_closed_posting_and_clips_long_text():
    text = summarize_job(job(posting_status="closed", description_text="x" * (DESCRIPTION_LIMIT + 50)))
    assert "Status: CLOSED" in text
    assert text.endswith("x" * 10 + "…") and "x" * (DESCRIPTION_LIMIT + 1) not in text
    assert summarize_job(job(description_text="")).endswith("Description:\n(none)")


def test_search_results_are_numbered_and_capped_at_ten():
    body = {"query": "acme", "results": [
        {"rank": i, "title": f"T{i}", "url": f"https://e.com/{i}", "snippet": "s"} for i in range(1, 15)]}
    text = format_search_results(body)
    assert text.startswith('Google results for "acme":')
    assert "10. T10\n   https://e.com/10\n   s" in text
    assert "T11" not in text


def test_search_with_no_results():
    assert format_search_results({"query": "zzz", "results": []}) == 'Google returned no results for "zzz".'


def test_search_results_skip_google_images_and_opaque_redirects():
    body = {"query": "acme", "results": [
        {"title": "img", "url": "https://encrypted-tbn0.gstatic.com/images?q=tbn:x"},
        {"title": "Acme", "url": "https://acme.com", "snippet": "anvils"},
        {"title": "goto", "url": "https://www.google.com/goto?url=CAESiAEB6zsw"},
        {"title": "Wrapped", "url": "https://www.google.com/url?q=https://example.com/a&sa=U"}]}
    text = format_search_results(body)
    assert "gstatic" not in text and "goto" not in text
    assert "1. Acme\n   https://acme.com\n   anvils" in text
    assert "2. Wrapped\n   https://example.com/a" in text
    assert "(Skipped 2 Google image or redirect links.)" in text


def search_snippet(url: str, snippet: str) -> str:
    text = format_search_results({"query": "q", "results": [{"title": "T", "url": url, "snippet": snippet}]})
    return text.split("\n")[3].strip()


def test_search_snippets_drop_the_site_name_and_link_that_repeat_the_result_link():
    url = "https://www.businesswire.com/news/home/20261006901425/en/Spiko-Raises"
    snippet = ("Web results Business Wire https://www.businesswire.com › home › 20261006901425 2 days ago — "
               "Spiko, the tokenized cash fund leader, today announced a $90 million Series B.")
    assert search_snippet(url, snippet) == (
        "2 days ago — Spiko, the tokenized cash fund leader, today announced a $90 million Series B.")
    assert search_snippet("https://fr.linkedin.com/jobs/view/1", "LinkedIn https://fr.linkedin.com › jobs › dév... · "
                          "Translate this page Plus de 1 000 offres") == "Plus de 1 000 offres"
    assert search_snippet("https://dealroom.co/news/1", "Dealroom https://dealroom.co › news › 158108-sealy-raises-40-5... "
                          "8 days ago — Sealy has closed") == "8 days ago — Sealy has closed"


def test_search_snippets_drop_the_text_of_google_buttons():
    url = "https://e.com/a"
    assert search_snippet(url, "led by names such as Zepto, Groww, Udaan and ...Read more") == (
        "led by names such as Zepto, Groww, Udaan and ...")
    assert search_snippet(url, "salary and equity. Missing: team ‎| Show results with: team") == "salary and equity."
    assert search_snippet(url, "a Seed ...Read more Missing: columsproutAI ‎website ‎Bengaluru") == "a Seed ..."
    assert search_snippet(url, "about 30+ employees in India.Read more 3.6(13)") == "about 30+ employees in India. 3.6(13)"
    assert search_snippet(url, "grown in just ...Read more Missing: headquarters ‎$22.5") == "grown in just ..."
    assert search_snippet(url, "cohorts that combine ... Missing: Magicroll. ‎Razorpay") == "cohorts that combine ..."


def test_search_snippets_keep_read_more_in_a_sentence():
    assert search_snippet("https://e.com/a", "Read more about our team. We hire.") == "Read more about our team. We hire."


def test_search_snippets_keep_a_link_that_is_content():
    # The link is to an other site: it is information, not a repeat of the result link.
    snippet = "LinkedIn · Devol Robots 1.5K+ followers Website: https://www.devolrobots.com/. Industry: Robotics"
    assert search_snippet("https://www.linkedin.com/company/devol", "Web results " + snippet) == snippet
    assert search_snippet("https://www.linkedin.com/in/x", "LinkedIn · Maka 1.4K+ followers Maka is") == (
        "LinkedIn · Maka 1.4K+ followers Maka is")


def test_search_with_only_unusable_links():
    body = {"query": "acme", "results": [{"title": "i", "url": "https://encrypted-tbn2.gstatic.com/faviconV2?url=x"}]}
    assert format_search_results(body) == 'Google returned no usable results for "acme" (only image or redirect links).'


