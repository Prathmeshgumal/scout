"""A summary of a goal's history, used when Claude's own session file is gone."""

from __future__ import annotations

MAX_ANSWER = 3000
MAX_FOLLOW_UPS = 5


def scrape_line(scrape: dict) -> str:
    outcome = scrape["status"] + (f" ({scrape['failure_kind']})" if scrape.get("failure_kind") else "")
    return f"- {scrape['request_id']} ({scrape['kind']}, {scrape['target']}): {outcome}"


def build_recap(events: list[dict], scrapes: list[dict]) -> str:
    asked = [e["text"] for e in events if e["type"] == "user_message"]
    answers = [e["text"] for e in events if e["type"] == "text" and e.get("agent", "main") == "main"]
    lines = ["Your earlier conversation for this mission was lost. This is a summary of it.",
             f"Mission: {asked[0] if asked else '(unknown)'}"]
    lines += [f"Later message from the user: {text}" for text in asked[1:][-MAX_FOLLOW_UPS:]]
    if answers:
        last = answers[-1]
        lines.append(f"Your last answer:\n{last if len(last) <= MAX_ANSWER else last[:MAX_ANSWER] + '…'}")
    if scrapes:
        lines.append("Scrapes so far:")
        lines += [scrape_line(scrape) for scrape in scrapes]
    return "\n".join(lines)
