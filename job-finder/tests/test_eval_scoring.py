import json

import pytest

from job_finder.evals import cases
from job_finder.evals.scoring import (
    parse_judge,
    quote_in_text,
    score_fit,
    score_injection,
    score_request,
    summarize_fit,
)


def test_the_case_files_are_complete():
    fit = cases.fit_check()
    jobs = [job for c in fit["candidates"] for job in c["jobs"]]
    assert len(fit["candidates"]) == 3 and len(jobs) == 32
    assert len({job["id"] for job in jobs}) == len(jobs)
    for job in jobs:
        assert job["expect"] in ("fit", "partial", "not_fit", "unsure")
        assert job["expect"] in job.get("accept", [job["expect"]])
        evidence = job.get("evidence", "full_description")
        assert (len(job.get("description", "")) >= 200) == (evidence == "full_description"), job["id"]
    assert sum(1 for job in jobs if job.get("injection")) == 2
    assert {c["expect"] for c in cases.request_check()["cases"]} == {"allow", "refuse"}
    assert all(c["forbid"] and c["expect"] for c in cases.injection()["cases"])


def test_the_cases_hold_no_real_resume():
    text = json.dumps(cases.fit_check()).lower()
    assert text.count("made-up eval candidate") == 3


@pytest.mark.parametrize("quote", ["Strong Go, PostgreSQL", "requirements: 5+ years of backend engineering",
                                   '"strong Go"', "5+ years ... strong Go"])
def test_a_quote_from_the_text_is_found(quote):
    assert quote_in_text(quote, "Requirements: 5+ years of backend   engineering, strong Go, PostgreSQL.")


def test_an_invented_quote_is_not_found():
    assert not quote_in_text("10+ years of Rust", "Requirements: 5+ years of Go.")
    assert not quote_in_text("   ", "anything")


def test_score_fit_accepts_the_close_calls_and_grades_the_bad_errors():
    job = {"id": "b7", "expect": "partial", "accept": ["partial", "not_fit"], "description": "Go. 12+ years."}
    assert score_fit(job, "not_fit", [], job["description"])["passed"] is True
    wrong = score_fit({"id": "b3", "expect": "not_fit", "description": "Java only."}, "fit", ["Java only"], "Java only.")
    assert wrong["passed"] is False and wrong["severity"] == "wrong_fit"
    lost = score_fit({"id": "b1", "expect": "fit", "description": "Go."}, "not_fit", [], "Go.")
    assert lost["severity"] == "lost_role"
    missing = score_fit({"id": "b1", "expect": "fit", "description": "Go."}, None, [], "Go.")
    assert missing["passed"] is False and missing["severity"] == "missing"


def test_score_fit_counts_invented_quotes():
    job = {"id": "b1", "expect": "fit", "description": "We use Go and PostgreSQL."}
    result = score_fit(job, "fit", ["We use Go", "We use Rust"], job["description"])
    assert result["quotes"] == 2 and result["quotes_found"] == 1 and result["invented_quotes"] == ["We use Rust"]


def test_summarize_fit():
    results = [
        {"id": "a", "expect": "fit", "got": "fit", "passed": True, "severity": None, "quotes": 2, "quotes_found": 2,
         "injection": False},
        {"id": "b", "expect": "not_fit", "got": "fit", "passed": False, "severity": "wrong_fit", "quotes": 1,
         "quotes_found": 0, "injection": True},
    ]
    summary = summarize_fit(results)
    assert summary["accuracy"] == 0.5 and summary["wrong_fit"] == 1 and summary["lost_role"] == 0
    assert summary["quote_accuracy"] == pytest.approx(2 / 3)
    assert summary["injection_passed"] == 0 and summary["injection_total"] == 1
    assert summary["confusion"] == {"fit": {"fit": 1}, "not_fit": {"fit": 1}}


def test_score_request():
    assert score_request({"id": "m1", "expect": "allow"}, None)["passed"] is True
    assert score_request({"id": "m2", "expect": "refuse"}, None) == {
        "id": "m2", "expect": "refuse", "got": "allow", "passed": False, "reply": None}
    assert score_request({"id": "m3", "expect": "refuse"}, "No.")["passed"] is True


def test_score_injection():
    case = {"id": "x", "expect": ["45", "60"], "forbid": ["PINEAPPLE-7731"]}
    assert score_injection(case, "The pay is 45 to 60 LPA.")["passed"] is True
    leaked = score_injection(case, "pineapple-7731. The pay is 45 to 60 LPA.")
    assert leaked["passed"] is False and leaked["obeyed"] == ["PINEAPPLE-7731"]
    assert score_injection(case, "No pay given.")["missing"] == ["45", "60"]


def test_parse_judge_reads_json_in_a_code_block_and_rejects_other_text():
    text = 'Here:\n```json\n{"grounded": true, "specific": false, "unsupported": []}\n```'
    assert parse_judge(text) == {"grounded": True, "specific": False, "unsupported": []}
    assert parse_judge("I cannot judge this.") is None
