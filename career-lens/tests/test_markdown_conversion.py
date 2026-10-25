"""HTML conversion stays independent of Docling's ML document stack."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


def test_html_conversion_works_without_pdf_or_ml_dependencies():
    root = Path(__file__).resolve().parent.parent
    program = """
import json
import sys

from career_lens.capture.markdown import to_markdown

for package in ("docling_parse", "pypdfium2", "torch"):
    sys.modules[package] = None
markdown = to_markdown(b\"<html><body><h1>Profile</h1><p>Hello world.</p></body></html>\")
print(json.dumps({\"markdown\": markdown}))
"""
    environment = os.environ | {"PYTHONPATH": str(root / "src")}

    completed = subprocess.run(
        [sys.executable, "-c", program],
        cwd=root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["markdown"] == "## Profile\n\nHello world."
