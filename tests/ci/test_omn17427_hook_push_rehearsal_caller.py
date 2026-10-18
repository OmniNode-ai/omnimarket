# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the hook push rehearsal caller runs on a hook change.

``.github/workflows/hook-push-rehearsal.yml`` calls omnibase_core's reusable
hook push rehearsal at an immutable full-SHA pin on every pull request and
merge-queue entry. Its ``path-regex`` matches every path that changes the
pre-push set (the pre-commit config, the hook scripts and the caller itself)
and no ordinary source path, and its ``sample-file`` is a Python source file in the package.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER = REPO_ROOT / ".github" / "workflows" / "hook-push-rehearsal.yml"
REUSABLE = "OmniNode-ai/omnibase_core/.github/workflows/hook-push-rehearsal.yml"


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert list(jobs) == ["hook-push-rehearsal"]
    job: dict[str, Any] = jobs["hook-push-rehearsal"]
    return job


def test_caller_runs_on_pull_request_and_merge_group() -> None:
    workflow = yaml.safe_load(CALLER.read_text(encoding="utf-8"))
    # PyYAML reads the bare ``on`` key as the boolean True.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"pull_request", "merge_group"}


def test_caller_pins_the_reusable_at_a_full_sha() -> None:
    uses = str(_job()["uses"])
    path, _, ref = uses.partition("@")
    assert path == REUSABLE
    assert re.fullmatch(r"[0-9a-f]{40}", ref)


@pytest.mark.parametrize(
    "changed_path",
    [
        ".pre-commit-config.yaml",
        ".pre-commit-hooks.yaml",
        ".pre-commit-hooks/check.py",
        "scripts/hooks/prepush_smart_tests.sh",
        ".github/workflows/hook-push-rehearsal.yml",
    ],
)
def test_path_regex_matches_hook_paths(changed_path: str) -> None:
    regex = str(_job()["with"]["path-regex"])
    assert re.search(regex, changed_path)


@pytest.mark.parametrize(
    "changed_path",
    ["src/omnimarket/adapters/adk/models.py", "README.md", "contracts/OMN-17427.yaml"],
)
def test_path_regex_skips_other_paths(changed_path: str) -> None:
    regex = str(_job()["with"]["path-regex"])
    assert not re.search(regex, changed_path)


def test_sample_file_is_a_python_source_file() -> None:
    sample = str(_job()["with"]["sample-file"])
    assert sample.startswith("src/omnimarket/")
    assert sample.endswith(".py")
    assert (REPO_ROOT / sample).is_file()
