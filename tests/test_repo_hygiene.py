"""Repository hygiene checks that CI enforces on every tracked source file.

These are deliberately simple and run without hardware: they read the files
`git ls-files` reports, nothing else.
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Hard ceiling on the length of any tracked Python source file. A module that
#: grows past this is a module that wants splitting, and reviewers (human or
#: automated) stop reading before the end of it.
MAX_LINES = 1000


def _tracked_python_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", REPO, "ls-files", "--", "*.py"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line.strip())


def _line_count(path: str) -> int:
    with open(os.path.join(REPO, path), encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def test_the_repo_tracks_python_files() -> None:
    assert _tracked_python_files(), "git ls-files returned no Python files"


@pytest.mark.parametrize("path", _tracked_python_files())
def test_no_python_file_exceeds_the_line_ceiling(path: str) -> None:
    lines = _line_count(path)
    assert lines <= MAX_LINES, (
        f"{path} is {lines} lines; the ceiling is {MAX_LINES}. Split the module "
        "instead of raising the limit."
    )
