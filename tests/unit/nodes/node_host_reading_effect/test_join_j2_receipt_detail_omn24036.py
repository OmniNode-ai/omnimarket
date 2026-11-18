# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24036: the seam_command_failed detail carries the receipt's own reason.

A pooled host that answers the canary with ``infra_error`` and a receipt detail
("worktree add failed: ... Permission denied") used to read back as "nonce not seen"
alone; the receipt's own detail now ends the failure text, and the step's
``failure_report`` line shows it too. A completed receipt with no detail keeps
today's text exactly.
"""

from __future__ import annotations

import pytest

from omnimarket.models.model_join_j2 import (
    EnumJoinJ2Outcome,
    ModelJoinJ2HostResult,
)
from omnimarket.nodes.node_host_reading_effect.handlers.join_j2_hosts import (
    failure_report,
)
from omnimarket.nodes.node_lab_work_unit_effect import (
    EnumLabWorkUnitStatus,
    ModelLabWorkUnitReceipt,
    ModelLabWorkUnitRequest,
)
from tests.unit.nodes.node_host_reading_effect.test_join_j2 import (
    POOLED,
    _by_host,
    _EchoRunner,
    _run,
    _World,
)

pytestmark = pytest.mark.unit

INFRA_DETAIL = (
    "worktree add failed: fatal: could not create leading directories "
    "of '/x/.git': Permission denied"
)


class _InfraErrorRunner(_EchoRunner):
    """Answers the canary the way a pool host with a broken worktree would."""

    def run(
        self, request: ModelLabWorkUnitRequest, host_name: str
    ) -> ModelLabWorkUnitReceipt:
        self.ran.append(request.work_unit_id)
        return ModelLabWorkUnitReceipt(
            work_unit_id=request.work_unit_id,
            target_host=request.target_host,
            host=host_name,
            repo=request.repo,
            commit_sha=request.commit_sha,
            lane=request.lane,
            kind=request.kind,
            status=EnumLabWorkUnitStatus.INFRA_ERROR,
            exit_code=None,
            output_tail="",
            detail=INFRA_DETAIL,
            duration_seconds=0.1,
        )


def test_error_a_host_receipt_detail_reaches_the_failure_text() -> None:
    """The receipt's own detail ends the failure text, result and report."""

    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _InfraErrorRunner())
        return await handler.run_hosts((POOLED[0],))

    _world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.COMMAND_FAILED
    assert result.failure_code == "seam_command_failed"
    assert "worktree add failed" in result.detail
    assert "Permission denied" in result.detail
    assert "Permission denied" in failure_report([result])


def test_golden_a_completed_receipt_with_no_detail_keeps_todays_text() -> None:
    """With no receipt detail the failure text is exactly what it was before."""

    async def scenario(world: _World) -> tuple[ModelJoinJ2HostResult, ...]:
        handler = await world.open()
        await world.serve(POOLED[0], _EchoRunner(exit_code=3))
        return await handler.run_hosts((POOLED[0],))

    _world, results = _run(scenario)
    result = _by_host(results)[POOLED[0]]
    assert result.outcome is EnumJoinJ2Outcome.COMMAND_FAILED
    assert result.failure_code == "seam_command_failed"
    assert result.detail == "status completed, exit 3, nonce seen"
