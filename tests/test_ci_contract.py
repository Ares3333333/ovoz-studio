"""The CI contract, checked where developers actually look: in the test suite.

`.github/workflows/ci.yml` is a claim about the repository, and a claim nobody
executes rots — the first real run of this pipeline was red on arrival (five ruff
findings that had been shipped for many rounds, and a trigger that named a branch
that did not exist). Running the same two commands here means the suite is red
before a push, not after.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

# `pip install ruff` — a tool version chosen by PyPI today instead of by us.
# A line that installs from a requirements file, or from a shell variable, is fine.
FLOATING_PIP = re.compile(r"pip install (?!-r|\$)\S")


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("ruff") is None and subprocess.run(
        [sys.executable, "-m", "ruff", "--version"], capture_output=True).returncode != 0,
        reason="ruff is not installed here")
def test_the_suites_lint_gate_is_the_same_command_ci_runs():
    """`ruff check app/` must be clean under the same selection CI uses.

    The point is not style: an unbound name in a helper is a NameError waiting for
    the one call path that touches it, and `--select E,F,W` catches the F family
    (undefined/unused names) that the rest of the suite cannot see.
    """
    src = _workflow_text()
    assert "ruff check app/" in src, "CI's lint command moved; update this gate"
    args = [sys.executable, "-m", "ruff", "check", "app/",
            "--select", "E,F,W", "--ignore", "E501"]
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, (r.stdout or "")[:2000]


def test_ci_triggers_on_the_branches_that_exist():
    """A trigger naming only a branch the repository does not have means zero
    checks and a green-looking commit that was never built."""
    src = _workflow_text()
    branches = set()
    for line in src.splitlines():
        if "branches:" in line:
            branches |= {b.strip().strip("[]'\" ")
                         for b in line.split(":", 1)[1].split(",")}
    heads = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                           cwd=ROOT, capture_output=True, text=True).stdout.strip()
    assert heads in branches, f"current branch {heads!r} would not run CI"
    assert {"main", "master"} <= branches, branches


def test_ci_installs_from_the_pinned_files_only():
    """A floating tool install makes CI's verdict depend on the day of the week.

    Every dependency of the pipeline comes from a pinned file, so a green run can
    be reproduced and a red one can be bisected to a commit, not to PyPI.
    """
    src = _workflow_text()
    floating = [line.strip() for line in src.splitlines() if FLOATING_PIP.search(line)]
    assert not floating, f"unpinned installs in CI: {floating}"


def test_ci_asks_the_questions_the_suite_asks():
    """CI runs the same suite with the same environment flag the sync-pipeline
    mode needs; a job that skips the flag tests a program nobody runs."""
    src = _workflow_text()
    assert "python -m pytest" in src
    assert "OVOZ_SYNC_PIPELINE" in src
    assert "docker build" in src, "the shipped image is no longer built by CI"
    assert "healthz" in src, "CI stopped asking whether the container answers"
