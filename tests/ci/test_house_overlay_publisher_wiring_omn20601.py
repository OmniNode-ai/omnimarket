# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20601: the house routing-overlay publisher test runs in CI and pre-commit.

The publisher test exists, so it only enforces anything when a workflow job
and a pre-commit hook run it. These tests fail when the ``lint`` job step or
the ``house-routing-overlay-publisher`` local hook stops naming the test file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"

PUBLISHER_TEST = (
    "tests/nodes/node_house_routing_overlay_effect/"
    "test_publish_house_overlay_command.py"
)
HOOK_ID = "house-routing-overlay-publisher"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), "expected a YAML mapping"
    return {str(key): item for key, item in value.items()}


def _mappings(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list), "expected a YAML list of mappings"
    return [_mapping(item) for item in value]


def test_publisher_test_file_exists() -> None:
    assert (REPO_ROOT / PUBLISHER_TEST).is_file()


def test_lint_job_runs_the_publisher_test() -> None:
    workflow = _mapping(yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8")))
    lint = _mapping(_mapping(workflow["jobs"])["lint"])
    runs = [
        step["run"]
        for step in _mappings(lint["steps"])
        if isinstance(step.get("run"), str)
    ]
    matching = [run for run in runs if PUBLISHER_TEST in str(run)]
    assert len(matching) == 1, f"lint job must run {PUBLISHER_TEST} exactly once"
    assert str(matching[0]).startswith("uv run pytest ")


def test_local_precommit_hook_runs_the_publisher_test() -> None:
    config = _mapping(yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8")))
    hooks = [
        (repo["repo"], hook)
        for repo in _mappings(config["repos"])
        for hook in _mappings(repo.get("hooks", []))
        if hook.get("id") == HOOK_ID
    ]
    assert len(hooks) == 1, f"expected exactly one {HOOK_ID} hook"
    repo, hook = hooks[0]
    assert repo == "local"
    entry = hook["entry"]
    assert isinstance(entry, str)
    assert entry.startswith("uv run pytest ")
    assert PUBLISHER_TEST in entry
    files = hook["files"]
    assert isinstance(files, str)
    assert "node_house_routing_overlay_effect" in files
