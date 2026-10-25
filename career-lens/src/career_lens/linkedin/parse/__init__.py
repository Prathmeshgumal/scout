"""The maintained LinkedIn profile parser.

This package is the
canonical implementation. Parser changes belong here together with matching
projection and migration changes. Legacy run-directory and CLI wrappers remain
outside this package because extraction reads durable capture artifacts.
"""

from __future__ import annotations

from .company import parse_company_html
from .parser import parse_profile_html

# What our projection was written against. Recorded on every extraction row, so
# a document produced by a different version is identifiable in the database
# rather than silently projected by rules meant for another shape.
#
# Upstream emits this as a string literal inside parser.py's return dict rather
# than as a named constant, so it cannot be imported. Checked against the
# document at projection time instead -- see ../project.py.
EXPECTED_VERSION = "2.2"
COMPANY_EXPECTED_VERSION = "1.1"

__all__ = [
    "parse_company_html",
    "parse_profile_html",
    "COMPANY_EXPECTED_VERSION",
    "EXPECTED_VERSION",
]
