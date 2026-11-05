import stat

import httpx
import pytest
import respx

from job_finder.login import SetupTokenView, TerminalOutput, find_token, login, save_credential
from job_finder.login import credential_works as real_credential_works

TOKEN = "sk-ant-oat01-Abc_def-123"
KEY = "sk-ant-api03-key_456"
URL = "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c&scope=user%3Ainference&state=-nhJcj_vS"
# Frames as `claude setup-token` writes them to a pipe: the screen again after each change, with the
# last character of the sign-in URL on a new line.
FRAME = ("\x1b[2mWelcome to Claude Code v2.1.291\x1b[22m\n\n This will guide you through long-lived (1-year) auth "
         "token setup.\n\n Browser didn't open? Use the url below to sign in (c to copy)\n\n" + URL[:-1] + "\n"
         + URL[-1] + "\n\n Paste code here if prompted > ")
# After a paste, the prompt shows one * for each character, on more lines when it is long.
PASTED = FRAME + "*" * 48 + "\n" + " " * 31 + "*" * 44 + "\n"
OUTPUT = (FRAME + FRAME + FRAME + "*" * 10 + "\n" + PASTED
          + "\n ✓ Long-lived authentication token created successfully!\n\n Your OAuth token (valid for 1 year):\n\n "
          + TOKEN + "\n\n Store this token securely.\n")


def env_file(tmp_path, text="# Claude\nANTHROPIC_API_KEY=old\nJOB_FINDER_PORT=8010\n"):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line and line[0] != "#")


def run(path, *, api_key=False, claude=True, output="", pasted="", environ=None):
    printed, calls = [], []

    def setup_token(out, code):
        calls.append("setup-token")
        return output

    code = login(path, api_key=api_key, which=lambda name: "/bin/claude" if claude else None,
                 setup_token=setup_token, ask=lambda text: (printed.append(text), pasted)[1],
                 out=lambda text, kind="info": printed.append(text), environ=environ if environ is not None else {})
    return code, "\n".join(printed), calls


def test_find_token_takes_the_last_token_in_the_output():
    assert find_token(OUTPUT.replace(TOKEN, TOKEN[:-3] + "old", 1) + TOKEN) == TOKEN
    assert find_token(OUTPUT) == TOKEN
    assert find_token("Login failed") is None


def test_the_setup_token_view_shows_the_url_and_the_paste_hint_once_and_never_the_token():
    shown, lengths = [], []
    view = SetupTokenView(lambda text, kind="info": shown.append(text), lengths.append)
    for start in range(0, len(OUTPUT), 7):  # the pipe gives the output in small parts
        view.feed(OUTPUT[start:start + 7])
    text = "\n".join(shown)
    assert text.count(URL) == 1 and text.count("paste it here") == 1
    assert TOKEN not in text and "Welcome" not in text and "*" not in text and "\x1b" not in text
    assert lengths[-1] == 92 and lengths == sorted(set(lengths))  # the pasted code: its length only, and it grows


def test_the_terminal_output_ends_the_code_line_and_keeps_a_long_url_in_one_line():
    import io

    from rich.console import Console
    file = io.StringIO()
    terminal = TerminalOutput(Console(file=file, width=40))
    terminal.code(5)
    terminal.code(92)
    terminal.out(URL, "url")
    terminal.out("✓ Saved.", "ok")
    lines = file.getvalue().split("\n")
    assert lines[0].endswith("Code: " + "*" * 40 + "… (92 characters)") and "\r" in lines[0]
    assert lines[1] == URL and lines[2] == "✓ Saved."


def test_a_token_is_saved_and_the_api_key_is_kept(tmp_path):
    path = env_file(tmp_path)
    assert save_credential(path, TOKEN) == "oauth_token"
    assert values(path) == {"ANTHROPIC_API_KEY": "old", "JOB_FINDER_PORT": "8010", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    assert path.read_text().startswith("# Claude\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_api_key_is_saved_and_the_token_is_kept(tmp_path):
    path = env_file(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    assert save_credential(path, KEY) == "api_key"
    assert values(path) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": KEY}


@pytest.mark.parametrize("value", ["", "hello", "sk-other"])
def test_other_values_are_not_saved(tmp_path, value):
    path = env_file(tmp_path)
    with pytest.raises(ValueError):
        save_credential(path, value)
    assert values(path)["ANTHROPIC_API_KEY"] == "old"


def test_login_saves_the_token_and_says_that_the_api_key_in_env_wins(tmp_path):
    path = env_file(tmp_path)
    code, printed, calls = run(path, output=OUTPUT)
    assert code == 0 and calls == ["setup-token"]
    assert values(path)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and values(path)["ANTHROPIC_API_KEY"] == "old"
    assert TOKEN not in printed and "old" not in printed
    assert "Saved the Claude subscription token to .env" in printed
    assert "ANTHROPIC_API_KEY is also set in .env" in printed and "Back up" in printed and "Comment out" in printed


def test_login_with_no_api_key_says_that_the_token_is_used(tmp_path):
    path = env_file(tmp_path, "# ANTHROPIC_API_KEY=old\n")
    code, printed, _ = run(path, output=OUTPUT)
    assert code == 0 and "ANTHROPIC_API_KEY is not set, so job-finder uses the subscription token" in printed


def test_login_asks_for_a_paste_when_claude_is_missing_or_gives_no_token(tmp_path):
    path = env_file(tmp_path, "")
    code, printed, calls = run(path, claude=False, pasted=TOKEN)
    assert code == 0 and calls == [] and "Claude Code is not installed" in printed
    assert values(path)["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    code, printed, calls = run(path, output="Login cancelled", pasted=KEY)
    assert code == 0 and calls == ["setup-token"] and "gave no token" in printed
    assert values(path)["ANTHROPIC_API_KEY"] == KEY and KEY not in printed


def test_login_with_api_key_asks_for_the_key_and_does_not_run_setup_token(tmp_path):
    path = env_file(tmp_path, f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}\n")
    code, printed, calls = run(path, api_key=True, pasted=KEY)
    assert code == 0 and calls == [] and "Anthropic API key" in printed
    assert values(path) == {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN, "ANTHROPIC_API_KEY": KEY}
    assert "job-finder uses the API key" in printed and KEY not in printed


@pytest.mark.parametrize("pasted,message", [("", "Nothing entered"), ("hello", "does not look like")])
def test_login_with_no_or_wrong_input_saves_nothing(tmp_path, pasted, message):
    path = env_file(tmp_path)
    code, printed, _ = run(path, claude=False, pasted=pasted)
    assert code == 1 and message in printed
    assert values(path) == {"ANTHROPIC_API_KEY": "old", "JOB_FINDER_PORT": "8010"}


def test_login_warns_when_the_shell_sets_an_api_key(tmp_path):
    _, printed, _ = run(env_file(tmp_path, ""), output=OUTPUT, environ={"ANTHROPIC_API_KEY": "sk-ant-from-shell"})
    assert "ANTHROPIC_API_KEY is set in your shell" in printed and "sk-ant-from-shell" not in printed
    _, printed, _ = run(env_file(tmp_path), output=OUTPUT, environ={"ANTHROPIC_API_KEY": "old"})  # loaded from .env
    assert "shell" not in printed


def test_run_setup_token_reads_the_real_process_output(tmp_path, monkeypatch):
    import sys

    from job_finder.login import run_setup_token
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\nimport sys\nsys.stdout.write({OUTPUT!r})\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{__import__('os').environ['PATH']}")
    shown = []
    output = run_setup_token(lambda text, kind="info": shown.append(text), lambda length: None)
    assert find_token(output) == TOKEN
    assert URL in "\n".join(shown) and TOKEN not in "\n".join(shown)


def test_in_the_container_the_restart_hint_names_the_compose_command():
    from job_finder.login import restart_hint
    assert "job-finder serve" in restart_hint({})
    assert "docker compose restart jobfinder" in restart_hint({"JOB_FINDER_ENV_FILE": "/config/.env"})


def test_login_in_the_container_says_to_restart_the_container(tmp_path):
    path = env_file(tmp_path, "")
    lines = []
    login(path, api_key=True, ask=lambda text: KEY, out=lambda text, kind="info": lines.append(text),
          environ={"JOB_FINDER_ENV_FILE": str(path)})
    assert any("docker compose restart jobfinder" in line for line in lines)


def test_as_root_the_saved_file_gets_the_owner_of_its_directory(tmp_path, monkeypatch):
    import os
    path = env_file(tmp_path)
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda file, uid, gid: calls.append((str(file), uid, gid)))
    save_credential(path, TOKEN)
    folder = tmp_path.stat()
    assert calls == [(str(path), folder.st_uid, folder.st_gid)]


def test_as_a_normal_user_the_saved_file_keeps_its_owner(tmp_path, monkeypatch):
    import os
    calls = []
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(os, "chown", lambda *args: calls.append(args))
    save_credential(env_file(tmp_path), TOKEN)
    assert calls == []


CHECK = "https://api.anthropic.com/v1/models"


@respx.mock
def test_the_check_sends_each_login_in_its_own_header():
    route = respx.get(CHECK).mock(return_value=httpx.Response(200, json={"data": []}))
    assert real_credential_works(KEY) is True
    assert route.calls.last.request.headers["x-api-key"] == KEY
    assert real_credential_works(TOKEN) is True
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_only_a_401_says_that_a_login_is_bad():
    respx.get(CHECK).mock(side_effect=[httpx.Response(401), httpx.Response(429), httpx.Response(403),
                                       httpx.Response(500), httpx.ConnectError("down")])
    assert [real_credential_works(KEY) for _ in range(5)] == [False, True, True, True, True]


def test_the_terminal_login_refuses_a_key_that_claude_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr("job_finder.login.credential_works", lambda value: False)
    lines = []
    path = tmp_path / ".env"
    code = login(path, api_key=True, ask=lambda _: KEY, out=lambda text, kind="info": lines.append(text))
    assert code == 1
    assert not path.exists()
    assert any("did not accept" in line for line in lines)
    assert not any(KEY in line for line in lines)
