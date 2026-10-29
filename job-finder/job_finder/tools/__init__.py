"""Tools that agents call. Each module serves one in-process MCP server."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..career_lens import CareerLens
from ..config import Settings
from ..events import Recorder
from ..page_reader import PageReader
from ..store import Store


def result(text: str, *, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if error:
        out["is_error"] = True
    return out


@dataclass
class ToolContext:
    goal_id: str
    store: Store
    recorder: Recorder
    career_lens: CareerLens
    poll_interval_s: float = 3.0
    poll_timeout_s: float = 300.0  # the longest that google_search and read_page wait for career-lens
    research_max_age_days: int = 30
    scrape_wait_s: float = 60.0  # the longest that wait_for_scrapes waits
    reader: PageReader | None = None  # answers the prompt of read_page
    settings: Settings | None = None  # the defaults of a new mandate
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    on_change: Callable[[], None] | None = None  # the goal became a home chat: reopen its session
    waiting: set[str] = field(default_factory=set)  # the session's: career-lens requests that a tool waits on now
