# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cloud placement handlers admit no direct harness invocation."""

from pathlib import Path

import pytest

from omnimarket.nodes.node_harness_invocation_check_effect.handlers.handler_harness_invocation_check import (
    HandlerHarnessInvocationCheck,
)
from omnimarket.nodes.node_harness_invocation_check_effect.models.model_harness_invocation_check import (
    ModelHarnessInvocationCheckRequest,
    ModelHarnessInvocationCheckResult,
)

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]
HANDLERS = "src/omnimarket/nodes/node_cloud_session_placement_effect/handlers"


def _check(repo_root: Path, *paths: str) -> ModelHarnessInvocationCheckResult:
    return HandlerHarnessInvocationCheck().handle(
        ModelHarnessInvocationCheckRequest(repo_root=str(repo_root), paths=paths)
    )


def test_placement_handlers_admit_no_direct_harness_invocation() -> None:
    paths = tuple(
        sorted(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / HANDLERS).rglob("*.py")
        )
    )
    assert paths
    assert f"{HANDLERS}/cloud_session.py" in paths
    assert f"{HANDLERS}/handler_cloud_session_placement.py" in paths

    result = _check(ROOT, *paths)

    assert result.admitted, "\n".join(result.output_lines)
    assert result.exit_code == 0
    assert result.refused == ()
    assert result.sites == ()


def test_placement_handlers_refuse_a_planted_harness_invocation(tmp_path: Path) -> None:
    rel = f"{HANDLERS}/planted.py"
    target = tmp_path / rel
    target.parent.mkdir(parents=True)
    target.write_text(
        'import subprocess\nsubprocess.run(["codex", "exec", "x"], check=True)\n',
        encoding="utf-8",
    )

    result = _check(tmp_path, rel)

    assert not result.admitted
    assert result.exit_code == 1
