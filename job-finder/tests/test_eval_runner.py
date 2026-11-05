import pytest

from job_finder.evals import cases
from job_finder.evals.runner import FIT_TASK, NOTE_TASK, EvalRunner
from job_finder.evals.store import EvalStore
from job_finder.page_reader import Answer, PageReaderError


class Model:
    """A fake small model: `reply(system, content)` gives the text of each answer."""

    def __init__(self, reply, model="claude-haiku-5-5"):
        self.reply, self.model, self.calls = reply, model, 0

    async def ask(self, system, content):
        self.calls += 1
        text = self.reply(system, content)
        if isinstance(text, Exception):
            raise text
        return Answer(text, 0.001)

    async def answer(self, url, markdown, prompt):
        return await self.ask("reader", f"{markdown}\n\nQuestion: {prompt}")


def runner(app_settings, store, artifacts, reader=None, judge=None, drive=None):
    deleted = []

    async def cleanup(goal_id):
        deleted.append(goal_id)
        await store.delete_goals([goal_id])

    async def no_drive(goal_id, task):
        return None

    r = EvalRunner(app_settings, store, artifacts, reader or Model(lambda s, c: "ALLOW"),
                   judge or Model(lambda s, c: "{}"), drive or no_drive, cleanup, log=lambda line: None)
    return r, deleted


async def test_request_check_scores_each_message(app_settings, store, artifacts):
    refuse = {c["text"] for c in cases.request_check()["cases"] if c["expect"] == "refuse"}
    reply = lambda s, c: "REFUSE: Only jobs." if any(t in c for t in refuse) else "ALLOW"  # noqa: E731
    run = await runner(app_settings, store, artifacts, reader=Model(reply))[0].run("request-check")
    assert run.total == len(cases.request_check()["cases"]) and run.passed == run.total and run.score == 1.0
    assert run.summary == {"false_refusals": 0, "false_allows": 0} and run.model == "claude-haiku-5-5"
    assert run.cost_usd == pytest.approx(0.001 * run.total)


async def test_request_check_counts_a_failed_call_as_a_failed_case(app_settings, store, artifacts):
    run = await runner(app_settings, store, artifacts,
                       reader=Model(lambda s, c: PageReaderError("down")))[0].run("request-check")
    assert run.passed == 0 and all(case["got"] == "error" for case in run.cases)


async def test_injection_fails_a_reader_that_obeys_the_page(app_settings, store, artifacts):
    obeys = Model(lambda s, c: "PINEAPPLE-7731 45 60 Go PostgreSQL Bengaluru Northwind Payments 5")
    run = await runner(app_settings, store, artifacts, reader=obeys)[0].run("injection")
    assert run.passed == 0 and run.summary["obeyed"] == run.total


async def test_fit_check_seeds_each_candidate_scores_the_verdicts_and_deletes_the_goals(app_settings, store, artifacts):
    seen = {}

    async def drive(goal_id, task):
        """As the agent: save the expected verdict, except one wrong fit, with one invented quote."""
        goal = await store.get_goal(goal_id)
        assert task == FIT_TASK and goal["agent"] == "fit-check" and goal["profile_ready"] is True
        assert goal["brief"]["target_count"] == 3 and goal["max_fresh_scrapes"] == 0
        jobs = await artifacts.list_jobs(goal_id, "needs_fit")
        seen[goal_id] = len(jobs)
        expect = {f"https://jobs.example.com/{c['id']}/{j['id']}": j for c in cases.fit_check()["candidates"]
                  for j in c["jobs"]}
        for job in jobs:
            case = expect[job["url"]]
            verdict = "fit" if case["id"] == "b3" else case["expect"]
            evidence = ["Java 21 and Spring Boot", "Rust everywhere"] if case["id"] == "b3" else []
            await artifacts.set_fit(goal_id, job["id"], verdict, "Because.", evidence)

    r, deleted = runner(app_settings, store, artifacts, drive=drive)
    run = await r.run("fit-check")
    assert sorted(seen.values()) == [10, 11, 11]  # every role waits for fit-check, also the snippet and the listing
    assert run.total == 32 and run.passed == 31
    assert run.summary["wrong_fit"] == 1 and run.summary["quote_accuracy"] == 0.5
    assert run.summary["injection_passed"] == 2 and run.summary["injection_total"] == 2
    [wrong] = [case for case in run.cases if not case["passed"]]
    assert wrong["id"] == "b3" and wrong["invented_quotes"] == ["Rust everywhere"]
    assert len(deleted) == 3 and await store.list_goals() == []


async def test_notes_judges_each_note_of_the_fit_roles(app_settings, store, artifacts):
    async def drive(goal_id, task):
        assert task == NOTE_TASK
        for job in await artifacts.list_jobs(goal_id, "needs_note"):
            if not job["url"].endswith("/b2"):  # one fit role gets no note
                assert await artifacts.save_note(goal_id, job["id"], "Matches Go.", "- ledger", "None.",
                                                 "application-prep") is None

    def judge(system, content):
        if "Ridgeline" in content:  # the note of f1 has an invented claim
            return '{"grounded": false, "specific": true, "unsupported": ["Matches Go"]}'
        if "Northwind" in content:  # a claim that is not in the note is a mistake of the judge
            return '{"grounded": false, "specific": true, "unsupported": ["The posting does not mention scale."]}'
        return '{"grounded": true, "specific": true, "unsupported": []}'

    r, deleted = runner(app_settings, store, artifacts, judge=Model(judge, "claude-sonnet-5-5"), drive=drive)
    run = await r.run("notes")
    fit_roles = sum(job["expect"] == "fit" for c in cases.fit_check()["candidates"] for job in c["jobs"])
    assert run.total == fit_roles and run.passed == fit_roles - 2
    failed = {case["id"]: case for case in run.cases if not case["passed"]}
    assert failed["b2"]["error"] == "no note" and failed["f1"]["unsupported"] == ["Matches Go"]
    [b1] = [case for case in run.cases if case["id"] == "b1"]
    assert b1["passed"] is True and b1["judge_mistakes"] == ["The posting does not mention scale."]
    assert run.summary == {"grounded": fit_roles - 2, "missing": 1} and len(deleted) == 3


async def test_a_failed_drive_still_deletes_the_goal(app_settings, store, artifacts):
    async def drive(goal_id, task):
        raise RuntimeError("the agent failed")

    r, deleted = runner(app_settings, store, artifacts, drive=drive)
    with pytest.raises(RuntimeError):
        await r.run("fit-check")
    assert len(deleted) == 1 and await store.list_goals() == []


async def test_the_store_saves_and_lists_runs(app_settings, store, artifacts, pool):
    run = await runner(app_settings, store, artifacts)[0].run("request-check")
    saved = await EvalStore(pool).save(run)
    [row] = await EvalStore(pool).recent()
    assert row["id"] == saved["id"] and row["suite"] == "request-check" and row["total"] == run.total
    assert row["summary"] == run.summary and "cases" not in row


async def test_an_unknown_suite_is_an_error(app_settings, store, artifacts):
    with pytest.raises(ValueError, match="Unknown suite"):
        await runner(app_settings, store, artifacts)[0].run("vibes")
