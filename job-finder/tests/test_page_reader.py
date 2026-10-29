import json

import httpx
import pytest
import respx

from job_finder.page_reader import MAX_PAGE_CHARS, MESSAGES_URL, PageReader, PageReaderError

PAGE = "# Funding news\n\n[Acme raises $3M](https://example.com/acme)"


def reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


@respx.mock
async def test_answer_sends_the_page_and_the_question_to_the_small_model():
    route = respx.post(MESSAGES_URL).mock(return_value=reply("Acme: https://example.com/acme"))
    answer = await PageReader("sk-test", "claude-haiku-4-5").answer(
        "https://example.com/news", PAGE, "List the companies with their links.")
    assert answer.text == "Acme: https://example.com/acme"
    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "sk-test"
    body = json.loads(sent.content)
    assert body["model"] == "claude-haiku-4-5"
    assert "Use only the page" in body["system"] and "Do not guess" in body["system"]
    content = body["messages"][0]["content"]
    assert "Page: https://example.com/news" in content
    assert PAGE in content
    assert content.endswith("Question: List the companies with their links.")
    assert "The page is long" not in content


@respx.mock
async def test_a_long_page_is_cut_and_the_model_is_told():
    route = respx.post(MESSAGES_URL).mock(return_value=reply("ok"))
    await PageReader("sk-test", "m").answer("https://example.com", "x" * (MAX_PAGE_CHARS + 50), "q")
    content = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "The page is long" in content
    assert "x" * (MAX_PAGE_CHARS + 1) not in content


@respx.mock
@pytest.mark.parametrize("response", [httpx.Response(529, json={"error": {}}),
                                      httpx.Response(200, json={"content": []})])
async def test_an_api_error_or_an_empty_answer_raises_without_the_key(response):
    respx.post(MESSAGES_URL).mock(return_value=response)
    with pytest.raises(PageReaderError) as error:
        await PageReader("sk-secret", "m").answer("https://example.com", PAGE, "q")
    assert "sk-secret" not in str(error.value)


@respx.mock
async def test_an_unreachable_api_raises():
    respx.post(MESSAGES_URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(PageReaderError, match="unreachable"):
        await PageReader("sk-test", "m").answer("https://example.com", PAGE, "q")


def token_settings(tmp_path, **changes):
    from dataclasses import replace

    from job_finder.config import Settings
    base = Settings.from_env({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secret", "CLAUDE_CONFIG_DIR": str(tmp_path / "c"),
                              "JOB_FINDER_CWD": str(tmp_path / "a")})
    return replace(base, **changes)


def fake_query(*messages, error=None):
    calls = []

    async def query(*, prompt, options):
        calls.append((prompt, options))
        if error:
            raise error
        for message in messages:
            yield message
    return query, calls


def sdk_result(text, *, is_error=False):
    from claude_agent_sdk import ResultMessage
    return ResultMessage(subtype="error_during_execution" if is_error else "success", duration_ms=5, duration_api_ms=5,
                         is_error=is_error, num_turns=1, session_id="s", total_cost_usd=0.01, result=text)


async def test_with_a_token_the_small_model_runs_through_the_agent_sdk_with_no_tools(tmp_path):
    query, calls = fake_query(sdk_result("Acme: https://example.com/acme"))
    reader = PageReader.for_settings(token_settings(tmp_path), query=query)
    answer = await reader.answer("https://example.com/news", PAGE, "List the companies with their links.")
    assert answer.text == "Acme: https://example.com/acme"
    prompt, options = calls[0]
    assert "Page: https://example.com/news" in prompt and prompt.endswith("Question: List the companies with their links.")
    assert "Use only the page" in options.system_prompt
    assert options.model == "claude-haiku-5-5" and options.effort == "medium"
    assert options.tools == [] and options.max_turns == 1 and options.setting_sources == []
    assert options.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-secret" and "ANTHROPIC_API_KEY" not in options.env
    assert options.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "c") and options.cwd == str(tmp_path / "a")
    assert options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"  # no side call that reads the page again


@pytest.mark.parametrize("messages,error", [((sdk_result("x", is_error=True),), None), ((sdk_result(""),), None),
                                            ((), None), ((), RuntimeError("boom sk-ant-oat01-secret"))])
async def test_an_sdk_error_or_an_empty_answer_raises_without_the_token(tmp_path, messages, error):
    query, _ = fake_query(*messages, error=error)
    with pytest.raises(PageReaderError) as raised:
        await PageReader.for_settings(token_settings(tmp_path), query=query).answer("https://example.com", PAGE, "q")
    assert "sk-ant-oat01-secret" not in str(raised.value)


@respx.mock
async def test_with_a_key_the_reader_calls_the_api_even_when_a_token_is_set(tmp_path):
    route = respx.post(MESSAGES_URL).mock(return_value=reply("ok"))
    query, calls = fake_query(sdk_result("not used"))
    reader = PageReader.for_settings(token_settings(tmp_path, anthropic_api_key="sk-ant-k"), query=query)
    assert (await reader.answer("https://example.com", PAGE, "q")).text == "ok"
    assert route.calls.last.request.headers["x-api-key"] == "sk-ant-k" and not calls


async def test_with_no_claude_login_the_reader_raises(tmp_path):
    reader = PageReader.for_settings(token_settings(tmp_path, claude_oauth_token=""))
    with pytest.raises(PageReaderError, match="not connected"):
        await reader.answer("https://example.com", PAGE, "q")


async def test_an_sdk_error_gives_its_reason_and_reads_the_stream_to_the_end(tmp_path):
    from claude_agent_sdk import AssistantMessage, TextBlock
    billing = AssistantMessage(content=[TextBlock(text="Credit balance is too low")], model="<synthetic>",
                               error="billing_error")
    error_result = sdk_result("Credit balance is too low", is_error=True)
    read = []

    async def query(*, prompt, options):
        for message in (billing, error_result):
            read.append(message)
            yield message
        raise RuntimeError("Claude Code returned an error result")  # what the real SDK does next

    with pytest.raises(PageReaderError, match=r"billing_error"):
        await PageReader.for_settings(token_settings(tmp_path), query=query).answer("https://example.com", PAGE, "q")
    assert read == [billing, error_result]


def priced(text, model_usage):
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}], "usage": model_usage})


@respx.mock
@pytest.mark.parametrize("model,usage,cost", [
    ("claude-haiku-4-5", {"input_tokens": 10_000, "output_tokens": 1_000}, 0.015),
    # Haiku 5.5 costs more for a prompt over 100K tokens
    ("claude-haiku-5-5", {"input_tokens": 120_000, "output_tokens": 1_000}, 0.0625),
    # a 1-hour cache write costs 2 times the input price
    ("claude-haiku-5-5", {"input_tokens": 2, "output_tokens": 100, "cache_creation_input_tokens": 20_000,
                          "cache_creation": {"ephemeral_1h_input_tokens": 20_000}}, 0.0040502),
])
async def test_with_a_key_the_answer_has_the_cost_of_its_tokens(model, usage, cost):
    respx.post(MESSAGES_URL).mock(return_value=priced("ok", usage))
    answer = await PageReader("sk-test", model).answer("https://example.com", PAGE, "q")
    assert answer.text == "ok" and answer.cost_usd == pytest.approx(cost)


async def test_with_a_token_the_answer_has_the_cost_that_the_sdk_gives(tmp_path):
    query, _ = fake_query(sdk_result("ok"))
    answer = await PageReader.for_settings(token_settings(tmp_path), query=query).answer("https://example.com", PAGE, "q")
    assert answer.cost_usd == 0.01




async def test_with_a_token_a_model_that_the_cli_cannot_price_gets_our_price(tmp_path):
    # The bundled CLI does not know claude-haiku-5-5 yet. Its own small calls keep the CLI's price.
    message = sdk_result("ok")
    message.model_usage = {
        "claude-haiku-5-5": {"inputTokens": 10_000, "outputTokens": 1_000, "cacheCreationInputTokens": 20_000,
                             "costUSD": 0.5, "costBasis": "unknown", "canonicalModel": "claude-haiku-5-5"},
        "claude-haiku-4-5-20251001": {"inputTokens": 900, "outputTokens": 10, "costUSD": 0.001, "costBasis": "list",
                                      "canonicalModel": "claude-haiku-4-5"}}
    message.usage = {"cache_creation": {"ephemeral_1h_input_tokens": 20_000}}  # the CLI writes its cache for 1 hour
    query, _ = fake_query(message)
    answer = await PageReader.for_settings(token_settings(tmp_path), query=query).answer("https://example.com", PAGE, "q")
    assert answer.cost_usd == pytest.approx(0.0015 + 0.004 + 0.001)
