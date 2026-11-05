"""What the API image is allowed to import.

The API container installs extraction and media-composition code but not
Patchright. It must be able to parse stored captures without acquiring a
browser or browser-profile dependency. `api.fleet` nonetheless reaches `capture.browser`
for one constant, so everything reachable from there has to stay importable
without the capture group.

No ordinary test catches a breach: a development checkout has all of it
installed, so the import succeeds here and fails only in the container. This
walks the import graph statically instead, which needs nothing installed at
all.

The breach this was written for: `capture/cursor.py` imported a constant from
`capture/page.py`, which imports lxml. The API restarted in a loop on
`ModuleNotFoundError: No module named 'lxml'`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
import tomllib

SRC = Path(__file__).resolve().parent.parent / "src"
PACKAGE = "career_lens"

#: Installed only in the host worker's `capture` dependency group.
CAPTURE_ONLY = {"patchright"}

#: Where the API starts. Anything these reach at import time must not need the
#: capture group.
API_ENTRY = ("career_lens.api.main",)


def _module_path(name: str) -> Path | None:
    parts = name.split(".")
    direct = SRC.joinpath(*parts).with_suffix(".py")
    if direct.exists():
        return direct
    package = SRC.joinpath(*parts, "__init__.py")
    return package if package.exists() else None


def _imports(path: Path, module: str) -> set[str]:
    """Module-level imports only -- an import inside a function is not paid."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()

    def package_of(name: str, level: int) -> str:
        """What `.` means inside `name`, for a relative import of `level`."""
        parts = name.split(".")
        # A module's own name is not part of its package, so one dot climbs to
        # its parent, two to its grandparent.
        keep = len(parts) - level
        return ".".join(parts[:keep]) if keep > 0 else PACKAGE

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            # Nothing nested in a function costs anything at import time, and
            # a TYPE_CHECKING block is never executed at all -- neither can
            # break the container.
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(child, ast.If) and _is_type_checking(child.test):
                continue
            if isinstance(child, ast.Import):
                found.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom):
                base = (package_of(module, child.level) if child.level
                        else "")
                if child.module:
                    found.add(f"{base}.{child.module}" if base else child.module)
                else:
                    # `from . import a, b` names submodules, not the package.
                    found.update(f"{base}.{alias.name}" for alias in child.names)
            visit(child)

    visit(tree)
    return found


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _reachable(entries: tuple[str, ...]) -> dict[str, str]:
    """Every module reachable at import time, mapped to who pulled it in."""
    seen: dict[str, str] = {}
    queue = [(name, "the API entry point") for name in entries]
    while queue:
        name, via = queue.pop()
        if name in seen:
            continue
        seen[name] = via
        path = _module_path(name)
        if path is None:
            continue
        for imported in _imports(path, name):
            if imported.startswith(PACKAGE):
                queue.append((imported, name))
            else:
                seen.setdefault(imported.split(".")[0], name)
    return seen


def test_the_api_never_reaches_the_capture_only_dependencies():
    reached = _reachable(API_ENTRY)

    breaches = {
        module: reached[module]
        for module in CAPTURE_ONLY if module in reached
    }

    assert not breaches, (
        "the API image does not install these, so importing them anywhere it "
        "reaches restarts the container in a loop:\n"
        + "\n".join(f"  {module}  (imported by {via})"
                    for module, via in sorted(breaches.items())))


def test_the_api_entry_imports_without_the_capture_dependency():
    """Exercise imports that the static graph cannot resolve through packages."""
    program = """
import builtins

real_import = builtins.__import__

def without_patchright(name, *args, **kwargs):
    if name == "patchright" or name.startswith("patchright."):
        raise ModuleNotFoundError("blocked browser dependency")
    return real_import(name, *args, **kwargs)

builtins.__import__ = without_patchright
from career_lens.api.main import create_app
assert create_app() is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True,
    )

    assert result.returncode == 0, result.stderr


def test_the_walk_would_notice_a_breach():
    # A guard that cannot fail is worse than none. `capture.page` imports lxml
    # at module level, so reaching it from the API is exactly the breach.
    reached = _reachable(("career_lens.capture.page",))

    assert "lxml" in reached


def test_the_shared_attribute_has_one_definition():
    # The fix was to move the constant somewhere both can import. If a second
    # definition appears, the strip and the thing being stripped can drift
    # apart silently.
    from career_lens.capture.cursor import ARTIFACT_ONLY as on_the_cursor
    from career_lens.capture.marks import ARTIFACT_ONLY as canonical
    from career_lens.capture.page import ARTIFACT_ONLY as used_by_the_strip

    assert canonical == used_by_the_strip == on_the_cursor

    source = (SRC / "career_lens" / "capture").glob("*.py")
    defined_in = [
        path.name for path in source
        if 'ARTIFACT_ONLY = "' in path.read_text(encoding="utf-8")
    ]
    assert defined_in == ["marks.py"]


def test_api_image_installs_extraction_without_browser_dependencies():
    root = Path(__file__).resolve().parent.parent
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    groups = project["dependency-groups"]
    dockerfile = (root / "Dockerfile.api").read_text(encoding="utf-8")

    assert groups["extract"] == [
        "docling-slim[format-html]>=2.119,<3",
        "lxml>=5.3",
        "pillow>=12.3.0",
    ]
    assert groups["capture"] == ["patchright>=1.64.2"]
    assert project["tool"]["uv"]["default-groups"] == [
        "dev", "capture", "extract",
    ]
    assert dockerfile.count("--group extract --no-default-groups") == 2
    assert "--group capture" not in dockerfile
