from job_finder.page_reader import Answer, PageReaderError
from job_finder.request_check import DEFAULT_REFUSAL, MAX_CHARS, SYSTEM, check_request


class Reader:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    async def ask(self, system, content):
        self.calls.append((system, content))
        if self.error:
            raise self.error
        return Answer(self.reply, 0.001)


async def test_a_job_search_is_allowed():
    reader = Reader("ALLOW")
    assert await check_request(reader, "Find 5 backend roles in Pune") is None
    assert reader.calls == [(SYSTEM, "<message>\nFind 5 backend roles in Pune\n</message>")]


async def test_other_work_is_refused_with_the_reason():
    reply = "REFUSE: I help only with a job search. Tell me the role that you want."
    assert await check_request(Reader(reply), "find me adult sites") == "I help only with a job search. Tell me the role that you want."


async def test_a_refusal_with_no_reason_gets_the_default_text():
    assert await check_request(Reader("refuse"), "write my homework") == DEFAULT_REFUSAL


async def test_a_failed_check_lets_the_goal_start():
    """The agents keep their prompt rules and the site policy, so a down check does not block the user."""
    assert await check_request(Reader(error=PageReaderError("down")), "Find jobs") is None
    assert await check_request(Reader("I am not sure"), "Find jobs") is None


async def test_a_long_message_is_cut():
    reader = Reader("ALLOW")
    await check_request(reader, "Find jobs. " + "resume " * 5000)
    assert len(reader.calls[0][1]) < MAX_CHARS + 50
