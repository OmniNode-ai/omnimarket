# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains for the dead-man nodes (OMN-20807).

The chain is the one the contracts declare: an evaluate command arrives as JSON,
the compute returns the ledger rows, a record command carries them to the
effect, and the effect writes them and publishes on the seam topics.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.liveness.model_automation_liveness_deadman import (
    ModelAutomationLivenessDeadmanRequest,
    ModelAutomationLivenessDeadmanResult,
)
from omnimarket.nodes.node_automation_liveness_deadman_compute.handlers.handler_automation_liveness_deadman import (
    HandlerAutomationLivenessDeadman,
)
from omnimarket.nodes.node_automation_liveness_deadman_effect.models.model_deadman_record import (
    ModelDeadmanRecordRequest,
    ModelDeadmanRecordResult,
)
from tests.nodes.node_automation_liveness_deadman_effect.support import (
    T0,
    FakeLedger,
    RecordingSink,
    make_effect,
    raised,
)

pytestmark = pytest.mark.unit


def test_deadman_golden_chain_record_command_to_declared_topics(tmp_path: Path) -> None:
    compute_command = ModelAutomationLivenessDeadmanRequest.model_validate_json(
        ModelAutomationLivenessDeadmanRequest(
            now=T0 + timedelta(minutes=10),
            primary_host="host-p",
            canary_host="host-p",
            primary_last_heartbeat_at=T0 + timedelta(minutes=10),
            opened=(raised("lab-fill", T0),),
        ).model_dump_json()
    )
    evaluated = HandlerAutomationLivenessDeadman().handle(compute_command)
    assert isinstance(evaluated, ModelAutomationLivenessDeadmanResult)
    assert len(evaluated.ledger_rows) == 1

    sink = RecordingSink()
    command = ModelDeadmanRecordRequest.model_validate_json(
        ModelDeadmanRecordRequest(
            now=T0 + timedelta(minutes=10),
            host="host-d",
            process_started_at=T0,
            rows=evaluated.ledger_rows,
        ).model_dump_json()
    )
    result = make_effect(tmp_path, FakeLedger(), sink).handle(command)
    assert isinstance(result, ModelDeadmanRecordResult)
    assert len(result.recorded) == 1
    assert len(sink.published) == 2


def test_deadman_error_chain_malformed_commands_never_reach_a_handler() -> None:
    with pytest.raises(ValidationError) as caught:
        ModelAutomationLivenessDeadmanRequest.model_validate({"opened": [{"x": 1}]})
    assert {err["loc"][0] for err in caught.value.errors()} >= {
        "now",
        "primary_host",
        "canary_host",
    }
    with pytest.raises(ValidationError):
        ModelDeadmanRecordRequest.model_validate({"rows": []})
