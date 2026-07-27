# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression test for the pre-existing OMN-13940 bug in workflow_runner.

Before this fix, ``_prepare_repair_worker_dispatch`` fired unconditionally in
``run_live_pr_polish`` -- even when the caller supplied an already-fixed
``worktree_path`` -- spawning a redundant repair-worker (Claude) agent spec
for a PR the delegated (non-Claude) fix path was explicitly trying to keep
off the agent path. ``skip_repair_dispatch=True`` must short-circuit that
call entirely.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_polish import workflow_runner
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_start_command import (
    ModelPrPolishStartCommand,
)
from omnimarket.nodes.node_pr_polish.models.model_pr_polish_state import (
    EnumPrPolishPhase,
)


@pytest.mark.unit
class TestSkipRepairDispatch:
    def test_skip_repair_dispatch_true_never_calls_prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        calls: list[object] = []

        def _fail_if_called(*args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))
            raise AssertionError(
                "_prepare_repair_worker_dispatch must not be called when "
                "skip_repair_dispatch=True"
            )

        monkeypatch.setattr(
            workflow_runner, "_prepare_repair_worker_dispatch", _fail_if_called
        )

        command = ModelPrPolishStartCommand(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=1,
            skip_repair_dispatch=True,
            dry_run=True,
            run_dir=str(tmp_path / "run"),
            requested_at=datetime.now(tz=UTC),
        )

        completed = workflow_runner.run_live_pr_polish(command)

        assert calls == []
        assert completed.final_phase == EnumPrPolishPhase.DONE
        assert completed.delegation_publish_status == "skipped_repair_dispatch"
        assert completed.repair_worker_payloads_prepared == 0
        assert completed.repair_workers_dispatched == 0
        assert completed.dispatch_worker_spec_path == ""
        # Contract-state-coverage gate (OMN-13781 baseline debt, surfaced by
        # touching node_pr_polish under OMN-13940): these two output fields
        # and the delegation-request publish topic had zero prior test
        # coverage anywhere in the suite.
        assert completed.dispatch_execution_result_path == ""
        assert completed.delegation_payloads_path == ""
        assert (
            "onex.cmd.omnimarket.delegation-request.v1"
            in Path(workflow_runner.__file__)
            .parent.joinpath("contract.yaml")
            .read_text()
        )

    def test_skip_repair_dispatch_false_still_calls_prepare(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Locks in existing behavior: default (False) still dispatches."""
        calls: list[object] = []

        def _record(command: object, *, run_dir: Path, started_at: object) -> dict:
            calls.append(command)
            return workflow_runner._skipped_repair_dispatch_evidence()

        monkeypatch.setattr(workflow_runner, "_prepare_repair_worker_dispatch", _record)

        command = ModelPrPolishStartCommand(
            correlation_id=uuid4(),
            repo="OmniNode-ai/omnimarket",
            pr_number=1,
            skip_repair_dispatch=False,
            dry_run=True,
            run_dir=str(tmp_path / "run"),
            requested_at=datetime.now(tz=UTC),
        )

        workflow_runner.run_live_pr_polish(command)

        assert len(calls) == 1
