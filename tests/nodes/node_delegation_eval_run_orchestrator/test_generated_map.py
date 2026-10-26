# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capability fold receives fresh rubric evidence with all six identities."""

from uuid import UUID

from omnimarket.models.delegation.wire.model_attempt_configuration import (
    ModelAttemptConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.nodes.node_projection_delegation_capability_map.generated_run import (
    build_generated_map_run,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_projection_delegation_capability_map import (
    HandlerProjectionDelegationCapabilityMap,
)
from tests.nodes.node_delegation_eval_run_orchestrator.test_backend_run import pin


def test_fresh_rubric_rows_fold_under_backend_and_refuse_small_cells() -> None:
    backend = pin()
    item = {
        "item_key": "i",
        "task_class": "document",
        "prompt_snapshot": "Explain a thing.",
        "response_snapshot": "A thing.",
        "error": None,
        "finish_reason": "stop",
        "backend_configuration": backend.model_dump(),
    }
    config = ModelCapabilityConfiguration(
        parameters=ModelAttemptConfiguration(
            max_tokens=4096, temperature=0, reasoning_effort="disabled"
        )
    )
    run, skipped = build_generated_map_run(
        UUID("11111111-1111-1111-1111-111111111111"),
        [item],
        backend,
        "manifest",
        config,
    )
    assert not skipped
    assert run.rows[0].backend_id == backend.backend_id
    assert not run.rows[0].identity.missing_fields()
    assert run.rows[0].configuration == config
    result = HandlerProjectionDelegationCapabilityMap().handle(run)
    assert result.folded_row_count == 1
    assert result.cells[0].verdict.value == "REFUSED"
    assert result.best_configurations == ()


def test_no_rubric_does_not_manufacture_a_cell() -> None:
    backend = pin()
    item = {
        "item_key": "i",
        "task_class": "reasoning",
        "prompt_snapshot": "p",
        "response_snapshot": "a",
        "error": None,
        "backend_configuration": backend.model_dump(),
    }
    run, skipped = build_generated_map_run(
        UUID("11111111-1111-1111-1111-111111111111"),
        [item],
        backend,
        "manifest",
        ModelCapabilityConfiguration(),
    )
    assert skipped == {"class_rubric_unavailable": 1}
    assert not run.rows
