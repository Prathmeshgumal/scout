import pytest
from rich.console import Console
from typer.testing import CliRunner

from job_finder.agents import REGISTRY
from job_finder.cli import CliError, app, list_goals, resolve_goal, run_agent, show_goal

from .fakes import Streams, make_services, replies, roles


@pytest.fixture
def streams():
    return Streams()


@pytest.fixture
def services(app_settings, store, hub, recorder, streams):
    return make_services(app_settings, store, hub, recorder, streams)


@pytest.fixture
def out():
    return Console(record=True, width=120)


async def test_resolve_goal(store):
    goal = await store.create_goal("g")
    assert (await resolve_goal(store, goal["id"][:8].upper()))["id"] == goal["id"]
    with pytest.raises(CliError, match="Not a mission id"):
        await resolve_goal(store, "xyz!")
    with pytest.raises(CliError, match="No mission starts with"):
        await resolve_goal(store, "ffffffff")


async def test_list_goals(services, out, store, artifacts):
    await list_goals(services, out)
    assert "No missions yet" in out.export_text()
    goal = await store.create_goal("Find Go roles")
    await artifacts.save_jobs(goal["id"], roles("Acme"), "job-discovery")
    await list_goals(services, out)
    text = out.export_text()
    assert goal["id"][:8] in text and "Find Go roles" in text and "main" in text


async def test_show_goal_and_notes(services, out, store, artifacts):
    goal = await store.create_goal("Find Go roles")
    await store.set_brief(goal["id"], {"criteria": "Go in Pune", "target_count": 5})
    [row] = await artifacts.save_jobs(goal["id"], roles("Acme"), "job-discovery")
    await show_goal(services, out, goal["id"][:8], notes=True)
    assert "No application notes yet." in out.export_text()
    await artifacts.save_job_research(goal["id"], row["job_id"], None, "snippet", None)
    await artifacts.set_fit(goal["id"], row["job_id"], "fit", "Go matches.", [])
    await artifacts.save_note(goal["id"], row["job_id"], "Six years of Go.", "Payments work.", "Kafka.",
                              "application-prep")
    await show_goal(services, out, goal["id"][:8])
    text = out.export_text()
    assert "Brief: 5 fit roles. Go in Pune" in text and "1 roles" in text and "Backend Engineer" in text
    await show_goal(services, out, goal["id"][:8], notes=True)
    text = out.export_text()
    assert "Backend Engineer, Acme" in text and "Six years of Go." in text and "Kafka." in text


async def test_show_research_prints_the_job_text_and_company_facts(services, out, store, artifacts):
    goal = await store.create_goal("Find jobs")
    acme, beta = await artifacts.save_jobs(goal["id"], roles("Acme", "Beta"), "job-discovery")
    await artifacts.save_company(goal["id"], acme["job_id"], name=None, website="acme.com", linkedin_url=None,
                                 data={"summary": "Anvils for [enterprises]", "size": "51-200", "funding": None},
                                 sources=["https://acme.com"], created_by="job-research")
    await show_goal(services, out, goal["id"][:8], research=True)
    text = out.export_text()
    assert "Backend Engineer, Acme" in text and "summary: Anvils for [enterprises]" in text and "funding: —" in text
    assert "sources: https://acme.com" in text and "Evidence: listing_only" in text
    assert "Backend Engineer, Beta" in text


async def test_run_agent_needs_a_known_subagent(services, out):
    with pytest.raises(CliError, match="Unknown subagent"):
        await run_agent(services, out, "nope", "task")


async def test_run_subagent_tasks_the_subagent_on_a_new_goal(services, out, store, streams):
    streams.on_query = replies("6 new roles.")
    await run_agent(services, out, "job-discovery", "Find 5 Go roles")
    [goal] = await store.list_goals()
    assert goal["agent"] == "job-discovery" and goal["status"] == "done"
    assert streams.last.options.system_prompt == REGISTRY["job-discovery"].prompt
    assert streams.last.queries == ["Find 5 Go roles"]
    assert "6 new roles." in out.export_text()


async def test_run_agent_refuses_a_goal_of_another_agent(services, out, store):
    goal = await store.create_goal("main mission")
    with pytest.raises(CliError, match="is not a job-research mission"):
        await run_agent(services, out, "job-research", "task", goal["id"])


async def test_run_agent_copies_roles(services, out, store, artifacts, streams):
    source = await store.create_goal("roles")
    await artifacts.save_jobs(source["id"], roles("Acme"), "job-discovery")
    streams.on_query = replies("1 full description.")
    await run_agent(services, out, "job-research", "Research the roles", None, source["id"][:8])
    dev = next(g for g in await store.list_goals() if g["agent"] == "job-research")
    assert [job["company_name"] for job in await artifacts.list_jobs(dev["id"])] == ["Acme"]
    assert "Copied 1 roles" in out.export_text()


def test_help_lists_the_commands():
    output = CliRunner().invoke(app, ["--help"]).output
    for command in ("chat", "missions", "show", "agent", "login"):
        assert command in output


def test_the_cli_needs_a_key_or_a_subscription_token(app_settings):
    from dataclasses import replace

    from job_finder.cli import need_key
    need_key(replace(app_settings, anthropic_api_key="", claude_oauth_token="sk-ant-oat01-t"))
    with pytest.raises(CliError, match="CLAUDE_CODE_OAUTH_TOKEN.*job-finder login"):
        need_key(replace(app_settings, anthropic_api_key=""))


def test_login_api_key_saves_the_pasted_key_to_the_env_file(tmp_path, monkeypatch):
    import job_finder.cli as cli
    env_file = tmp_path / ".env"
    monkeypatch.setattr(cli, "ENV_FILE", env_file)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = CliRunner().invoke(app, ["login", "--api-key"], input="sk-ant-api03-k\n\n")
    assert result.exit_code == 0 and "Saved the API key to .env" in result.output
    assert "sk-ant-api03-k" not in result.output
    assert "ANTHROPIC_API_KEY=sk-ant-api03-k" in env_file.read_text()


def test_login_saves_the_account_label_without_showing_the_key(tmp_path, monkeypatch):
    import json

    import job_finder.cli as cli
    monkeypatch.setattr(cli, "ENV_FILE", tmp_path / ".env")
    result = CliRunner().invoke(app, ["login", "--api-key", "--account-label", "Work account"],
                                input="sk-ant-api03-work\n")
    assert result.exit_code == 0 and "sk-ant-api03-work" not in result.output
    metadata = json.loads((tmp_path / ".env.login.json").read_text())
    assert metadata["api_key"]["account_label"] == "Work account"


async def test_mandate_commands(services, out, store, pool):
    from datetime import UTC, datetime, time

    from job_finder.cli import list_mandates, show_mandate
    from job_finder.mandate_store import MandateStore
    await list_mandates(services, out)
    assert "No schedules yet" in out.export_text()
    home = await store.create_goal("home")
    m = await MandateStore(pool).create(home["id"], {
        "title": "EU fintech jobs", "instruction": "fintech", "every_unit": "day", "every_count": 1, "jobs_per_run": 20,
        "window_days": [1, 2, 3, 4, 5], "window_start": time(9), "timezone": "UTC", "max_spend_per_run": 2.0,
        "max_scrapes_per_run": 15, "next_run_at": datetime(2030, 1, 1, tzinfo=UTC)})
    await list_mandates(services, out)
    assert "EU fintech jobs" in out.export_text()
    await show_mandate(services, out, m["id"][:8])
    assert "Status: active" in out.export_text()


def test_the_cli_cannot_change_a_mandate():
    # Only the agent of the mandate's chat changes a mandate, after the user says yes.
    from job_finder.cli import mandate_app
    assert sorted(c.name for c in mandate_app.registered_commands) == ["delete", "list", "show"]


async def test_delete_commands_ask_first(services, out, store, make_run):
    from job_finder.cli import delete_goal, delete_mandate

    goal = await store.create_goal("Find Go roles")
    await delete_goal(services, out, goal["id"][:8], False, lambda text: False)
    assert "Nothing is deleted." in out.export_text() and await store.get_goal(goal["id"]) is not None
    await delete_goal(services, out, goal["id"][:8], False, lambda text: True)
    assert await store.get_goal(goal["id"]) is None
    mandate, run = await make_run()
    with pytest.raises(CliError, match="job-finder schedule delete"):
        await delete_goal(services, out, mandate["home_goal_id"], True)
    await delete_mandate(services, out, mandate["id"][:8], True)
    text = out.export_text()
    assert "1 mission" in text and "Deleted." in text and await store.get_goal(run["id"]) is None


async def test_run_evals_refuses_an_unknown_suite(services, out):
    from job_finder.cli import run_evals
    with pytest.raises(CliError, match="Unknown suite: vibes"):
        await run_evals(services, out, ["vibes"])


async def test_run_evals_saves_each_run_and_prints_the_failures(services, out, pool):
    from job_finder.cli import run_evals
    from job_finder.evals.store import EvalRun, EvalStore

    class Runner:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self, suite):
            return EvalRun(suite=suite, model="m", prompt_sha="abc123", total=2, passed=1, score=0.5, cost_usd=0.01,
                           cases=[{"id": "x1", "passed": True},
                                  {"id": "x2", "passed": False, "expect": "fit", "got": "not_fit",
                                   "severity": "lost_role"}])

    runs = await run_evals(services, out, ["fit-check", "injection"], runner_class=Runner)
    assert [run.suite for run in runs] == ["fit-check", "injection"]
    assert {row["suite"] for row in await EvalStore(pool).recent()} == {"fit-check", "injection"}
    text = out.export_text()
    assert "failed x2: expected fit, got not_fit; lost_role" in text and "1/2" in text and "50%" in text


def test_help_lists_the_eval_command():
    assert "eval" in CliRunner().invoke(app, ["--help"]).output
