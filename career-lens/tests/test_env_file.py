"""One env file, one reading: the supervisor sees what the containers see."""

from __future__ import annotations

import pytest

from career_lens.store.config import ConfigError


def test_an_unquoted_json_keyring_keeps_its_inner_quotes():
    """The failure this exists for.

    `set -a; source .env; set +a` removed these quotes and left the keyring
    unparseable, while docker compose read the same line correctly.
    """
    from career_lens.store.env_file import parse_env_file

    values = parse_env_file('CAREER_LENS_CREDENTIAL_KEYS={"1":"abc"}\n')

    assert values["CAREER_LENS_CREDENTIAL_KEYS"] == '{"1":"abc"}'


def test_single_quotes_are_stripped_without_touching_the_value():
    from career_lens.store.env_file import parse_env_file

    values = parse_env_file("""KEYS='{"1":"a/b+c="}'\n""")

    assert values["KEYS"] == '{"1":"a/b+c="}'


def test_double_quoted_values_expand_escapes_and_single_quoted_ones_do_not():
    from career_lens.store.env_file import parse_env_file

    values = parse_env_file('A="one\\ntwo"\nB=\'one\\ntwo\'\n')

    assert values["A"] == "one\ntwo"
    assert values["B"] == "one\\ntwo"


def test_comments_blank_lines_and_export_are_understood():
    from career_lens.store.env_file import parse_env_file

    values = parse_env_file(
        "# a comment\n"
        "\n"
        "export A=1\n"
        "B=2   # trailing comment\n"
        "C=keep#this\n")

    assert values == {"A": "1", "B": "2", "C": "keep#this"}


def test_a_quoted_value_may_span_lines():
    from career_lens.store.env_file import parse_env_file

    values = parse_env_file("A='first\nsecond'\nB=2\n")

    assert values == {"A": "first\nsecond", "B": "2"}


def test_an_unterminated_quote_is_refused_by_name():
    from career_lens.store.env_file import parse_env_file

    with pytest.raises(ConfigError) as caught:
        parse_env_file("A='never closed\n")

    assert "A opens a ' that never closes" in str(caught.value)


def test_a_line_that_is_not_an_assignment_is_refused_with_its_number():
    from career_lens.store.env_file import parse_env_file

    with pytest.raises(ConfigError) as caught:
        parse_env_file("A=1\nnot an assignment\n", source="weekend.env")

    assert "weekend.env line 2 is not NAME=value" in str(caught.value)


def test_loading_sets_what_is_missing_and_keeps_what_the_shell_already_set(
        tmp_path):
    from career_lens.store.env_file import load_env_file

    path = tmp_path / ".env"
    path.write_text("A=from-file\nB=from-file\n", encoding="utf-8")
    environ = {"A": "from-shell"}

    applied, kept = load_env_file(path, environ)

    assert environ == {"A": "from-shell", "B": "from-file"}
    assert applied == ["B"]
    assert kept == ["A"]


def test_a_missing_env_file_says_so_rather_than_starting_without_it(tmp_path):
    from career_lens.store.env_file import load_env_file

    with pytest.raises(ConfigError) as caught:
        load_env_file(tmp_path / "absent.env", {})

    assert "cannot read" in str(caught.value)
