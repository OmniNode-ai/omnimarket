# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20910: the Hostile Reviewer reads omniintelligence by its newest dev tag and carries no key.

omniintelligence tags every dev merge ``dev-<UTC>-<sha12>``. A consumer reads that tag, so no pull
request waits on a hand-bumped sha pin. The workflow resolves the newest ``refs/tags/dev-*`` with
``git ls-remote`` at run time, logs it and clones at it. A branch tip stays refused: a moving ref
that is not a tag would let an unreviewed push reach the reviewer's own code.

A Claude API voter's key is a team provider key: it reaches the voter by reference through the
tenant route (OMN-20974), never from an Actions secret, so no step of the job carries it.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)
KEY_ENV = "ANTHROPIC_API_KEY"
KEY_EXPR = "${{ secrets.HOSTILE_REVIEW_ANTHROPIC_API_KEY }}"
REVIEW_STEP_ANCHOR = "          BASE_SHA: ${{ github.event.pull_request.base.sha }}\n"
CLONE_STEP = "Clone omniintelligence"
_CLONE_CALL = re.compile(
    r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA (\S+)"
)
_RESOLVER = re.compile(
    r"^(?P<indent> *)newest_dev_tag\(\) \{\n.*?^(?P=indent)\}\n", re.S | re.M
)


def _git(*args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        check=True,
        env=scrub_git_location_env(os.environ),
    )


def _remote_with_tags(path: Path, tags: tuple[str, ...]) -> None:
    _git("init", "-q", "-b", "dev", str(path))
    _git("-C", str(path), "commit", "-q", "--allow-empty", "-m", "c")
    for tag in tags:
        _git("-C", str(path), "tag", tag)


def steps_of(workflow_text: str) -> list[dict[str, Any]]:
    parsed = yaml.safe_load(workflow_text)
    steps = parsed["jobs"]["hostile-review"]["steps"]
    assert isinstance(steps, list)
    return [dict(s) for s in steps]


def clone_run(workflow_text: str) -> str:
    named = [s for s in steps_of(workflow_text) if s.get("name") == CLONE_STEP]
    assert len(named) == 1
    return str(named[0]["run"])


def tag_problems(workflow_text: str) -> list[str]:
    """Why the omniintelligence clone does not read the newest dev tag."""
    run = clone_run(workflow_text)
    problems: list[str] = []
    if not re.search(r"git ls-remote .*refs/tags/dev-\*", run):
        problems.append("the newest refs/tags/dev-* is not resolved with git ls-remote")
    if not re.search(r'echo ".*\$\{?OMNIINTELLIGENCE_TAG\}?', run):
        problems.append("the resolved tag is not logged")
    refs = _CLONE_CALL.findall(run)
    if refs != ['"refs/tags/${OMNIINTELLIGENCE_TAG}"']:
        problems.append(f"omniintelligence is not cloned at the resolved tag: {refs}")
    return problems


def key_problems(workflow_text: str) -> list[str]:
    """The steps that carry the Claude API key, which only the tenant route may supply."""
    return [
        f"step {step.get('name')!r} carries {KEY_ENV}"
        for step in steps_of(workflow_text)
        if (step.get("env") or {}).get(KEY_ENV) is not None
    ]


def test_omniintelligence_is_cloned_at_its_newest_dev_tag() -> None:
    assert tag_problems(WORKFLOW.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("tip", ["dev", "main", "refs/heads/dev", "HEAD"])
def test_a_branch_tip_is_still_refused(tip: str) -> None:
    """Positive control: the clone reading a branch tip instead of the resolved tag."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace('"refs/tags/${OMNIINTELLIGENCE_TAG}"', tip)
    assert tag_problems(before) == [
        f"omniintelligence is not cloned at the resolved tag: ['{tip}']"
    ]


def test_a_sha_literal_is_not_the_tag_read() -> None:
    """Positive control: the workflow as it was, pinned to a sha."""
    text = WORKFLOW.read_text(encoding="utf-8")
    sha = "7f686d4bbca251dd3db49c1222ff8d6f738abd6c"
    before = text.replace('"refs/tags/${OMNIINTELLIGENCE_TAG}"', sha)
    assert any(sha in p for p in tag_problems(before))


def _run_resolver(remote: Path) -> subprocess.CompletedProcess[str]:
    match = _RESOLVER.search(clone_run(WORKFLOW.read_text(encoding="utf-8")))
    assert match, "no newest_dev_tag function in the clone step"
    function = textwrap.dedent(match.group(0))
    script = f'set -euo pipefail\n{function}\nnewest_dev_tag "file://{remote}"\n'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=scrub_git_location_env(os.environ),
    )


def test_the_resolver_picks_the_newest_dev_tag_and_never_a_branch_tip(
    tmp_path: Path,
) -> None:
    """Run the workflow's own resolver against a repository with several tags."""
    _remote_with_tags(
        tmp_path / "remote",
        (
            "dev-20261018065049-63e412d2bd0a",
            "dev-20261018085529-fe44102f0bf5",
            "dev-20261018081711-c0cdfceade8b",
            "dev-20261019000000-notasha",
            "v1.2.3",
            "release-20270101000000-aaaaaaaaaaaa",
        ),
    )
    result = _run_resolver(tmp_path / "remote")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "dev-20261018085529-fe44102f0bf5"


def test_the_resolver_fails_when_no_dev_tag_exists(tmp_path: Path) -> None:
    """Positive control for the zero: a dev branch and no dev tag is an error."""
    _remote_with_tags(tmp_path / "remote", ("v1.2.3",))
    result = _run_resolver(tmp_path / "remote")
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_no_step_carries_the_claude_api_key() -> None:
    assert key_problems(WORKFLOW.read_text(encoding="utf-8")) == []


def test_the_key_on_the_review_step_is_caught() -> None:
    """Positive control: the Actions-secret wiring OMN-20974 refuses, on the review step."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count(REVIEW_STEP_ANCHOR) == 1
    leaked = text.replace(
        REVIEW_STEP_ANCHOR, REVIEW_STEP_ANCHOR + f"          {KEY_ENV}: {KEY_EXPR}\n"
    )
    assert key_problems(leaked) == [f"step 'Run adversarial review' carries {KEY_ENV}"]
