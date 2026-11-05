from job_finder.recap import build_recap, scrape_line


def test_scrape_line_shows_failure_kind():
    assert scrape_line({"request_id": "rq-2", "kind": "linkedin_person", "target": "u", "status": "failed",
                        "failure_kind": "blocked"}) == "- rq-2 (linkedin_person, u): failed (blocked)"


def test_recap_has_goal_follow_ups_last_answer_and_scrapes():
    events = [
        {"type": "user_message", "text": "Find the VP of Sales at Acme"},
        {"type": "text", "text": "Looking."},
        {"type": "text", "text": "Jane Doe is VP Sales (https://www.linkedin.com/in/jane/)."},
        {"type": "user_message", "text": "Also check Globex"},
    ]
    scrapes = [{"request_id": "rq-1", "kind": "linkedin_person", "target": "u", "status": "succeeded",
                "failure_kind": None}]
    assert build_recap(events, scrapes).splitlines() == [
        "Your earlier conversation for this mission was lost. This is a summary of it.",
        "Mission: Find the VP of Sales at Acme",
        "Later message from the user: Also check Globex",
        "Your last answer:",
        "Jane Doe is VP Sales (https://www.linkedin.com/in/jane/).",
        "Scrapes so far:",
        "- rq-1 (linkedin_person, u): succeeded",
    ]
