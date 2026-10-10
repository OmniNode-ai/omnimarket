# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: the local runner checks out the earlier commit from a shallow clone.

The evidence sweep checks product repositories out at depth 1. A ``--shared``
clone of a shallow repository silently drops its alternates, so the throwaway
tree cannot read the pre-change commit the runner just fetched into the source,
and every control came back as an infrastructure error instead of a verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.delegated_test_loop.must_fail_control import ModelMustFailRunRequest
from omnimarket.delegated_test_loop.must_fail_local_tree_run import (
    HandlerMustFailLocalTreeRun,
)
from tests.unit.delegated_test_loop.test_must_fail_local_tree_run_omn20032 import (
    _git,
    _provider,
    _repo,
)

pytestmark = pytest.mark.unit


def _shallow_clone(tmp_path: Path, repo: Path) -> Path:
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow))
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    return shallow


def test_a_shallow_source_still_runs_the_test_on_the_earlier_code(
    tmp_path: Path,
) -> None:
    repo, pre, change = _repo(tmp_path, test_passes_before=False)
    shallow = _shallow_clone(tmp_path, repo)

    result = HandlerMustFailLocalTreeRun(_provider).handle(
        ModelMustFailRunRequest(
            repo_dir=shallow,
            pre_change_sha=pre,
            change_sha=change,
            test_path="tests/test_widget.py",
            overlay_paths=("tests/test_widget.py",),
            timeout_seconds=120,
        )
    )

    assert "checkout failed" not in result.detail, result.detail
    assert result.exit_code == 1, result
    assert 'failures="1"' in result.junit_xml
