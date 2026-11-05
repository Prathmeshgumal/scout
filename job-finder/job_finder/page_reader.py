"""A small model that answers a question from one web page, as the built-in WebFetch tool does."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage
from claude_agent_sdk import query as sdk_query

from .config import Settings, claude_env

MESSAGES_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
MAX_PAGE_CHARS = 200_000  # characters of a page that the small model reads
MAX_ANSWER_TOKENS = 8192  # Haiku 5.5 thinks first, and its thinking counts here too
# USD for 1M input and output tokens. A cache write costs 1.25 times the input price (2 times for a 1-hour write),
# a cache read 0.1 times.
# A model with a long-prompt price has it as a third item: (prompt tokens over which it applies, input, output).
PRICES: dict[str, tuple] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50, (100_000, 0.50, 2.50)),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-5-5": (4.00, 20.00),
}
NO_EFFORT = {"claude-haiku-4-5"}  # these models reject the effort setting
log = logging.getLogger(__name__)


def read_effort(model: str, effort: str) -> str | None:
    """The effort to send: only to models with a known price that accept it. An unknown model gets none."""
    return effort if effort and model in PRICES and model not in NO_EFFORT else None

SYSTEM = """You read one web page and answer a question about it.

- Use only the page. Do not add facts from other sources.
- Use the words of the page for facts, names and numbers.
- Keep the URLs of the links that you use in your answer.
- If the page does not give the answer, say that. Do not guess.
- Give only the answer. Do not describe the page or your method."""


def sdk_cost(message: ResultMessage) -> float:
    """The cost of an SDK run. The CLI prices a model that it does not know at a guess ("unknown"): use our price."""
    if not message.model_usage:
        return message.total_cost_usd or 0.0
    total = 0.0
    hour = ((message.usage or {}).get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0
    for name, used in message.model_usage.items():
        model = used.get("canonicalModel") or name
        if used.get("costBasis") == "unknown" and model in PRICES:
            write = used.get("cacheCreationInputTokens") or 0  # model_usage does not split the writes by duration
            total += api_cost(model, {"input_tokens": used.get("inputTokens"), "output_tokens": used.get("outputTokens"),
                                      "cache_creation_input_tokens": write,
                                      "cache_creation": {"ephemeral_1h_input_tokens": min(hour, write)},
                                      "cache_read_input_tokens": used.get("cacheReadInputTokens")})
        else:
            total += used.get("costUSD") or 0.0
    return total


class PageReaderError(Exception):
    """The small model did not answer. The message never contains the API key or the token."""

    def __init__(self, message: str, *, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd  # a failed answer can cost money too


@dataclass(frozen=True)
class Answer:
    text: str
    cost_usd: float  # the goal's spend includes it


def api_cost(model: str, usage: dict) -> float:
    """The USD cost of a Messages API response, from its usage. 0 for a model with no known price."""
    price = PRICES.get(model)
    if price is None:
        log.warning("no price for the page reader model %s: its cost is not in the mission spend", model)
        return 0.0
    write, read = usage.get("cache_creation_input_tokens") or 0, usage.get("cache_read_input_tokens") or 0
    hour = min((usage.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0, write)
    tokens_in, tokens_out = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    per_in, per_out = price[0], price[1]
    if len(price) > 2 and tokens_in + write + read > price[2][0]:
        per_in, per_out = price[2][1], price[2][2]
    writes = (write - hour) * 1.25 + hour * 2
    return (tokens_in * per_in + writes * per_in + read * per_in * 0.1 + tokens_out * per_out) / 1_000_000


def clip(text: str, limit: int) -> tuple[str, bool]:
    """The text cut to `limit` characters, and True when it was cut."""
    return (text, False) if len(text) <= limit else (text[:limit], True)


class PageReader:
    """With an API key, it calls the Messages API. With only a subscription token, it runs one turn through the
    Agent SDK: the token is for Claude Code, not for direct API calls."""

    def __init__(self, api_key: str, model: str, *, timeout_s: float = 120.0, sdk_env: dict[str, str] | None = None,
                 cwd: str | None = None, query=sdk_query, effort: str = ""):
        self.api_key = api_key
        self.model = model
        self.effort = read_effort(model, effort)
        self.timeout_s = timeout_s
        self.sdk_env = sdk_env  # None: no subscription token
        self.cwd = cwd
        self._query = query

    @classmethod
    def for_settings(cls, settings: Settings, *, query=sdk_query, model: str | None = None) -> PageReader:
        """model: another model than the read model, e.g. the main model for an eval judge."""
        model = model or settings.read_model
        if settings.auth_mode != "oauth_token":
            return cls(settings.anthropic_api_key, model, effort=settings.read_effort)
        # Without the setting, Claude Code also sends the whole page to Haiku 4.5 in a side call, at 10 times the price.
        env = {"CLAUDE_CONFIG_DIR": str(settings.claude_config_dir), "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
               **claude_env(settings)}
        return cls("", model, sdk_env=env, cwd=str(settings.agent_cwd), query=query,
                   effort=settings.read_effort)

    async def answer(self, url: str, markdown: str, prompt: str) -> Answer:
        page, cut = clip(markdown, MAX_PAGE_CHARS)
        note = f"\nThe page is long. You have only its first {MAX_PAGE_CHARS} characters." if cut else ""
        content = f"Page: {url}{note}\n\n<page>\n{page}\n</page>\n\nQuestion: {prompt}"
        return await self.ask(SYSTEM, content)

    async def ask(self, system: str, content: str) -> Answer:
        """One answer of the small model, with this system prompt."""
        if self.api_key:
            return await self._api_answer(system, content)
        if self.sdk_env is not None:
            return await self._sdk_answer(system, content)
        raise PageReaderError("the page reader cannot run: Claude is not connected")

    async def _sdk_answer(self, system: str, content: str) -> Answer:
        options = ClaudeAgentOptions(system_prompt=system, model=self.model, effort=self.effort, tools=[],
                                     max_turns=1, setting_sources=[], permission_mode="dontAsk", env=self.sdk_env,
                                     cwd=self.cwd)
        got = {"text": "", "error": None, "cost": 0.0}

        async def run() -> None:  # read to the end: the SDK closes Claude Code when its stream ends
            async for message in self._query(prompt=content, options=options):
                if isinstance(message, AssistantMessage) and message.error:
                    got["error"] = message.error
                elif isinstance(message, ResultMessage):
                    got["cost"] = sdk_cost(message)
                    if message.is_error:
                        got["error"] = got["error"] or message.subtype
                    else:
                        got["text"] = (message.result or "").strip()

        try:
            await asyncio.wait_for(run(), self.timeout_s)
        except TimeoutError:
            raise PageReaderError("the page reader took too long", cost_usd=got["cost"]) from None
        except Exception:
            if not got["error"]:  # the error text can hold the token, so do not show it
                raise PageReaderError("the page reader failed", cost_usd=got["cost"]) from None
        if got["error"]:
            raise PageReaderError(f"the page reader failed ({got['error']})", cost_usd=got["cost"])
        if not got["text"]:
            raise PageReaderError("the page reader gave no answer", cost_usd=got["cost"])
        return Answer(got["text"], got["cost"])

    async def _api_answer(self, system: str, content: str) -> Answer:
        payload = {"model": self.model, "max_tokens": MAX_ANSWER_TOKENS, "system": system,
                   "messages": [{"role": "user", "content": content}]}
        if self.effort:
            payload["output_config"] = {"effort": self.effort}
        headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION}
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                response = await client.post(MESSAGES_URL, json=payload, headers=headers)
        except httpx.RequestError as exc:
            raise PageReaderError("the page reader is unreachable") from exc
        if response.status_code >= 400:
            raise PageReaderError(f"the page reader failed (HTTP {response.status_code})")
        body = response.json()
        cost = api_cost(self.model, body.get("usage") or {})
        if body.get("stop_reason") == "refusal":
            raise PageReaderError("the page reader declined to read this page", cost_usd=cost)
        text = "".join(block.get("text", "") for block in body.get("content") or [] if block.get("type") == "text").strip()
        if not text:
            raise PageReaderError("the page reader gave no answer", cost_usd=cost)
        return Answer(text, cost)
