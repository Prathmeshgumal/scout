"""The case files of the evals. All people, resumes and companies in them are made up."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

FOLDER = Path(__file__).parent / "cases"


@cache
def _load(name: str) -> dict:
    return json.loads((FOLDER / f"{name}.json").read_text())


def fit_check() -> dict:
    return _load("fit_check")


def injection() -> dict:
    return _load("injection")


def request_check() -> dict:
    return _load("request_check")
