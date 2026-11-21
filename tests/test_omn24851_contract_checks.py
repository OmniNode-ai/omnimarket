# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fails-on-base check of the OMN-24851 contract can pass on a post-merge checkout (OMN-24851 contract).

The check runs the new test file against the pinned base runner and passes only
on the exact set of failures that run produces. The file gained a tenth test
after the check was written, so a check that pins nine failures can never pass.
These tests run the contract's own check text, with the two pinned objects and
``uv`` replaced by a throwaway repository and a stub that prints a canned run.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-24851.yaml"
_ITEM = "dod-omn24851-ac2-fails-on-base"
_FILE = "tests/delegation_golden/test_runner_unterminalised_row_omn24831.py"
_PREFIX = f"FAILED {_FILE}::TestAnUnterminalisedRowIsNamedAsSuch::"
_NULL_IDS = ("I1", "I10", "I3", "I5", "I6", "I7", "I8", "I9")
_NULL = [
    f"{_PREFIX}test_one_failure_names_the_null_terminal_ok[{i}]" for i in _NULL_IDS
]
_ABSENT = f"{_PREFIX}test_an_absent_terminal_ok_key_is_reported_as_absent"
_TELEMETRY = f"{_PREFIX}test_it_does_not_report_telemetry_misses_it_cannot_judge"
_BASE_PIN = "6efbe3ee5027f6efb67d0b81a1421f0dda32b4e5"


def _item() -> dict[str, Any]:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return next(i for i in document["dod_evidence"] if i["id"] == _ITEM)


def _check() -> str:
    return str(_item()["checks"][0]["check_value"])


def _run_check(tmp_path: Path, failed_lines: list[str], summary: str) -> int:
    """Run the contract's check text against a throwaway repo and a stub ``uv``."""
    repo = tmp_path / "repo"
    (repo / "tests" / "delegation_golden").mkdir(parents=True)
    (repo / "tests" / "delegation_golden" / "runner.py").write_text("# stub\n")
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            env={**scrub_git_location_env(os.environ), **identity},
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "fixture")
    sha = git("rev-parse", "HEAD")

    canned = tmp_path / "canned.txt"
    canned.write_text("\n".join([*failed_lines, summary]) + "\n")
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    stub.write_text(f'#!/bin/sh\ncat "{canned}"\nexit 1\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    run_env = {
        **scrub_git_location_env(os.environ),
        **identity,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
    }
    run_env.pop("PYTHONPATH", None)

    return subprocess.run(
        ["bash", "-c", _check_text_for(sha)],
        cwd=repo,
        env=run_env,
        capture_output=True,
        text=True,
    ).returncode


def _check_text_for(sha: str) -> str:
    """Point both pinned objects of the check text at the throwaway commit."""
    text = _check()
    text = text.replace(f"b={_BASE_PIN}", f"b={sha}")
    return re.sub(r"\bh=[0-9a-f]{40}", f"h={sha}", text)


@pytest.mark.unit
def test_the_check_pins_both_objects_and_does_not_read_head() -> None:
    text = _check()
    assert f"b={_BASE_PIN}" in text
    assert re.search(r"\bh=[0-9a-f]{40};", text), text
    # A tree read from HEAD moves with dev, which is how the nine became ten.
    assert "git archive HEAD" not in text
    assert 'git archive "$h"' in text


@pytest.mark.unit
def test_the_description_states_the_count_the_check_enforces() -> None:
    description = str(_item()["description"])
    assert "exactly ten failures" in description
    assert "exactly nine" not in description
    assert "test_an_absent_terminal_ok_key_is_reported_as_absent" in description


@pytest.mark.unit
def test_the_check_passes_on_the_ten_failures_the_base_runner_produces(
    tmp_path: Path,
) -> None:
    code = _run_check(tmp_path, [*_NULL, _ABSENT, _TELEMETRY], "10 failed, 105 passed")
    assert code == 0


@pytest.mark.unit
def test_the_check_refuses_the_nine_failures_it_used_to_expect(
    tmp_path: Path,
) -> None:
    code = _run_check(tmp_path, [*_NULL, _TELEMETRY], "9 failed, 106 passed")
    assert code != 0


@pytest.mark.unit
def test_the_check_refuses_a_run_with_an_eleventh_failure(tmp_path: Path) -> None:
    extra = f"{_PREFIX}test_some_new_test"
    code = _run_check(
        tmp_path, [*_NULL, _ABSENT, _TELEMETRY, extra], "11 failed, 104 passed"
    )
    assert code != 0


@pytest.mark.unit
@pytest.mark.parametrize("dropped", [*_NULL, _ABSENT, _TELEMETRY])
def test_the_check_refuses_a_run_that_lacks_any_one_expected_id(
    tmp_path: Path, dropped: str
) -> None:
    failed = [line for line in [*_NULL, _ABSENT, _TELEMETRY] if line != dropped]
    # Ten failed overall, so only the id check can refuse it.
    failed.append(f"{_PREFIX}test_some_other_failure")
    assert _run_check(tmp_path, failed, "10 failed, 105 passed") != 0


@pytest.mark.unit
def test_the_check_refuses_a_bare_failing_run(tmp_path: Path) -> None:
    assert _run_check(tmp_path, [], "1 failed, 114 passed") != 0
