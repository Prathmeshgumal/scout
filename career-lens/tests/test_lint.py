"""Static checks, as part of the suite.

There is no CI here, so a linter nobody runs is a linter that does not exist.
Running it as a test means it runs whenever anything else does.

What this catches that the rest of the suite structurally cannot: a name that
is used but never imported, in a function no test reaches. `exhaust_list`
called `scroll_through` without importing it and stayed that way indefinitely,
because nothing called `exhaust_list` at all -- the failure needed a live
browser and a real profile to appear. Coverage-independent checks are the only
kind that find those.

Scoped to correctness (pyflakes plus syntax errors). Style is deliberately not
enforced: this codebase places its line breaks by hand and its comments carry
reasoning, and a formatter would rewrite both for no defect.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("src", "tests", "scripts")


def _ruff() -> str:
    """The ruff that belongs to this environment, not whatever is on PATH."""
    beside = Path(sys.executable).parent / "ruff"
    if beside.exists():
        return str(beside)
    found = shutil.which("ruff")
    if found:
        return found
    pytest.fail(
        "ruff is not installed in this environment. It is a dev dependency: "
        "run `uv sync` and try again. Skipping it silently would defeat the "
        "point of having it.")


def test_no_undefined_names_anywhere():
    # The check that pays for this file. It reads every module without
    # importing or executing any of it, so a broken name in an unreached branch
    # is found the same as one on the happy path.
    result = subprocess.run(
        [_ruff(), "check", "--select", "F821", "--output-format", "concise",
         *TARGETS],
        cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, (
        "a name is used that is never defined or imported:\n\n"
        f"{result.stdout}{result.stderr}")


def test_the_tree_passes_its_configured_lint():
    # The rule set lives in pyproject.toml so an editor and this test cannot
    # disagree about what counts.
    result = subprocess.run(
        [_ruff(), "check", "--output-format", "concise", *TARGETS],
        cwd=ROOT, capture_output=True, text=True)

    assert result.returncode == 0, (
        f"ruff reported problems:\n\n{result.stdout}{result.stderr}")


def test_the_lint_would_actually_fail_on_a_bad_name():
    # A lint test that cannot fail is worse than none: it reports safety it is
    # not providing. This proves the wiring by giving ruff something broken.
    broken = ROOT / "tests" / "_lint_probe_tmp.py"
    broken.write_text("def f():\n    return not_a_real_name\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [_ruff(), "check", "--select", "F821", "--output-format", "concise",
             str(broken)],
            cwd=ROOT, capture_output=True, text=True)
    finally:
        broken.unlink(missing_ok=True)

    assert result.returncode != 0
    assert "F821" in result.stdout


def test_every_module_can_be_imported():
    """Import each module in the package, to catch import-time breakage.

    Ruff finds a name that is used and never defined. It cannot find an import
    that resolves to nothing at runtime -- a re-export deleted as "unused",
    which is how `session.py` lost `type_text` to an over-eager autofix, or a
    module whose dependencies moved. Neither shows up until something imports
    it, and a module no test imports never gets the chance.
    """
    import importlib
    import pkgutil

    import career_lens

    broken: list[str] = []
    for module in pkgutil.walk_packages(career_lens.__path__, "career_lens."):
        # Alembic's env is executed by alembic with a context this does not
        # have; importing it standalone is meaningless rather than broken.
        if module.name.endswith("migrations.env"):
            continue
        try:
            importlib.import_module(module.name)
        except Exception as error:                       # noqa: BLE001
            broken.append(f"  {module.name}: {type(error).__name__}: {error}")

    assert not broken, "modules that will not import:\n" + "\n".join(broken)
