# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus orchestrator's gate-result consumer accepts a port-path gate record.

The port publishes its record to the topic the bus orchestrator also consumes.
The consumer validates the bus result shape with extra fields forbidden, so a
record with its own fields must be recognised and acknowledged, not refused as
an invalid message and not fed to the workflow.
"""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from omnibase_infra.enums import EnumDispatchStatus

from omnimarket.models.delegation.wire.model_quality_gate_record import (
    ModelQualityGateRecord,
    quality_gate_record_key,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    DispatcherQualityGateResult,
)


def _record_payload() -> dict[str, object]:
    correlation_id = uuid4()
    record = ModelQualityGateRecord(
        record_key=quality_gate_record_key(correlation_id, 0),
        correlation_id=correlation_id,
        attempt_index=0,
        task_type="code_generation",
        passed=True,
        fail_category="pass",
        quality_score=1.0,
        checks_skipped=("passes_existing_tests",),
        deciding_rule="accepted",
    )
    return record.model_dump(mode="json")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_port_gate_record_accepted_by_bus_consumer() -> None:
    handler = MagicMock()
    dispatcher = DispatcherQualityGateResult(handler)

    result = await dispatcher.handle({"payload": _record_payload()})

    assert result.status == EnumDispatchStatus.SUCCESS
    assert result.output_events == []
    handler.handle_gate_result.assert_not_called()
