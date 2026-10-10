# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation terminal mapping into the ledger append contract."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_work_ledger_delegation_mirror import (
    HandlerWorkLedgerDelegationMirror,
)

pytestmark = pytest.mark.unit

RUN = UUID("6dd0aa7a-a16e-497c-9e35-83a46cda6404")


@pytest.mark.parametrize("status", ["completed", "failed", "timeout"])
def test_delegation_terminal_maps_to_status(status: str) -> None:
    terminal = ModelDelegateSkillTerminalProjection.model_validate(
        {
            "correlation_id": RUN,
            "status": status,
            "task_type": "document",
            "model_name": "lab-model",
            "quality_gate_passed": status == "completed",
            "caller_lane": "test-lane",
            "emitted_at": datetime(2026, 10, 9, 0, 10, tzinfo=UTC),
        }
    )
    mapper = HandlerWorkLedgerDelegationMirror()
    request = mapper.handle(terminal)
    cells = request.rows.split(" | ")
    assert cells[:2] == ["2026-10-09T00:10:00Z", "STATUS"]
    for cell in (
        "lane=test-lane",
        "src=delegation",
        f"run={RUN}",
        "model=lab-model",
        f"outcome={status}",
        f"quality_gate_passed={str(status == 'completed').lower()}",
        f"req={request.request_id}",
    ):
        assert cell in cells
    assert request.request_id.version == 5
    assert mapper.handle(terminal) == request
    assert request.requested_by_lane == "test-lane"
    assert request.requested_at == terminal.emitted_at


def test_mapping_escapes_cells_without_copying_prompt_or_response() -> None:
    terminal = ModelDelegateSkillTerminalProjection.model_validate(
        {
            "correlation_id": RUN,
            "status": "failed",
            "task_type": "document",
            "model_name": "model | outcome=completed\nsecond row",
            "caller_lane": "lane | actor=operator",
            "prompt_text": "private prompt",
            "response": "private response",
        }
    )
    request = HandlerWorkLedgerDelegationMirror().handle(terminal)
    assert len(request.rows.splitlines()) == 1
    assert " | outcome=completed" not in request.rows
    assert " | actor=operator" not in request.rows
    assert "private" not in request.rows
