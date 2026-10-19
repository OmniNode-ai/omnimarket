# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic recorded-cascade controls; these are not model evaluation results."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    default_response_topic,
)
from omnimarket.nodes.node_context_gate_cascade_compute.handlers.handler_gate_cascade import (
    HandlerContextGateCascade,
)
from omnimarket.nodes.node_context_gate_cascade_compute.models.model_gate_cascade import (
    ModelGateCascadeRequest,
)

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_context_gate_cascade_compute"
)


def request_data() -> dict:
    return {
        "profile_id": "synthetic-profile",
        "role": "extraction",
        "gate": "laya",
        "winner_rung_id": "synthetic-winner",
        "winner_evidence_id": "synthetic-winner-evidence",
        "label_set_id": "synthetic-labels",
        "gate_training_evidence_id": "synthetic-training-evidence",
        "work_unit_repository": "example/public-repo",
        "repository_visibility": "public",
        "case_ids": ["positive", "negative"],
        "observations": [
            {
                "case_id": case,
                "repeat": repeat,
                "expected_label_ids": ["label-a", "label-b"]
                if case == "positive"
                else [],
                "ungated_recalled_ids": ["label-a", "label-b"]
                if case == "positive"
                else [],
                "cascade_recalled_ids": ["label-a", "label-b"]
                if case == "positive"
                else [],
                "gate_admitted": case == "positive",
                "ungated_cost_usd": "0.10",
                "gate_cost_usd": "0.01",
                "downstream_cost_usd": "0.10" if case == "positive" else "0",
            }
            for case in ("positive", "negative")
            for repeat in (1, 2, 3)
        ],
    }


def score(data: dict):
    return HandlerContextGateCascade().handle(
        ModelGateCascadeRequest.model_validate(data)
    )


def test_golden_cascade_measures_cost_and_preserves_evidence() -> None:
    result = score(request_data())
    assert result.recommended
    assert result.recall_equal_everywhere
    assert result.profile_id == "synthetic-profile"
    assert result.role == "extraction"
    assert result.gate == "laya"
    assert result.winner_rung_id == "synthetic-winner"
    assert result.winner_evidence_id == "synthetic-winner-evidence"
    assert result.label_set_id == "synthetic-labels"
    assert result.gate_training_evidence_id == "synthetic-training-evidence"
    assert result.ungated_cost_usd == Decimal("0.60")
    assert result.cascade_cost_usd == Decimal("0.36")
    assert result.cost_saved_usd == Decimal("0.24")
    assert len(result.observations) == 6
    assert all(row.recall_equal for row in result.observations)
    assert all(
        row.ungated_recall is None and row.cascade_recall is None
        for row in result.observations
        if row.expected_count == 0
    )
    assert score(request_data()) == result


@pytest.mark.parametrize("repeat", [1, 2, 3])
def test_one_recall_loss_refuses_recommendation(repeat: int) -> None:
    data = request_data()
    row = next(
        row
        for row in data["observations"]
        if row["case_id"] == "positive" and row["repeat"] == repeat
    )
    row["cascade_recalled_ids"] = ["label-a"]
    result = score(data)
    assert not result.recommended
    assert not result.recall_equal_everywhere
    failed = [row for row in result.observations if not row.recall_equal]
    assert [(row.case_id, row.repeat) for row in failed] == [("positive", repeat)]
    assert failed[0].ungated_recall == 1
    assert failed[0].cascade_recall == 0.5


def test_improvement_is_not_exact_parity_and_savings_include_gate_cost() -> None:
    data = request_data()
    data["observations"][0]["ungated_recalled_ids"] = ["label-a"]
    assert not score(data).recommended
    data = request_data()
    for row in data["observations"]:
        row["gate_cost_usd"] = "0.20"
    result = score(data)
    assert result.recall_equal_everywhere
    assert result.cost_saved_usd < 0
    assert not result.recommended


@pytest.mark.parametrize("visibility", ["private", "unknown"])
def test_jev_refuses_nonpublic_before_execution(visibility: str) -> None:
    data = request_data() | {"gate": "jev", "repository_visibility": visibility}
    with pytest.raises(
        ValidationError, match="Jev requires public repository attribution"
    ):
        ModelGateCascadeRequest.model_validate(data)


@pytest.mark.parametrize("gate", ["kev-9b", "kev-27b", "semif", "laya", "jev"])
def test_all_gate_classes_and_selection_role(gate: str) -> None:
    result = score(request_data() | {"gate": gate, "role": "selection"})
    assert result.gate == gate
    assert result.role == "selection"
    assert result.recommended


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "extra_case",
        "bad_repeat",
        "invented_label",
        "skipped_recall",
        "skipped_cost",
        "negative_cost",
        "nan_cost",
        "missing_training",
    ],
)
def test_incomplete_or_inconsistent_evidence_is_refused(mutation: str) -> None:
    data = request_data()
    row = data["observations"][0]
    if mutation == "missing":
        data["observations"].pop()
    elif mutation == "duplicate":
        data["observations"].append(deepcopy(row))
    elif mutation == "extra_case":
        row["case_id"] = "unlisted"
    elif mutation == "bad_repeat":
        row["repeat"] = 4
    elif mutation == "invented_label":
        row["cascade_recalled_ids"] = ["invented"]
    elif mutation == "skipped_recall":
        row["gate_admitted"] = False
    elif mutation == "skipped_cost":
        data["observations"][-1]["downstream_cost_usd"] = "0.10"
    elif mutation == "negative_cost":
        row["gate_cost_usd"] = "-1"
    elif mutation == "nan_cost":
        row["gate_cost_usd"] = "NaN"
    else:
        data["gate_training_evidence_id"] = None
    with pytest.raises(ValidationError):
        ModelGateCascadeRequest.model_validate(data)


def test_gate_prose_cannot_enter_or_leave_scoring_schema() -> None:
    data = request_data()
    data["observations"][0]["gate_output"] = "GATE-PROSE-SENTINEL"
    with pytest.raises(ValidationError, match="gate_output"):
        ModelGateCascadeRequest.model_validate(data)
    data["observations"][0].pop("gate_output")
    result = score(data)
    assert "GATE-PROSE-SENTINEL" not in result.model_dump_json()
    assert "gate_output" not in result.model_dump()
    assert "package_text" not in result.model_dump()


def test_order_independent_scoring() -> None:
    data = request_data()
    expected = score(data)
    data["observations"].reverse()
    data["case_ids"].reverse()
    assert score(data) == expected


def test_contract_topics_and_output_states_are_bound() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.context-gate-cascade-evaluated.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.context-gate-cascade-evaluated.v1"
    )
    assert (
        contract["runtime_dispatch"]["command_topic"]
        == contract["event_bus"]["subscribe_topics"][0]
    )
    assert set(contract["outputs"]) == set(score(request_data()).model_dump())


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["public", "private", "unknown"])
async def test_lab_runtime_dispatches_contract_and_rejects_private_jev(
    tmp_path: Path, visibility: str
) -> None:
    runtime = LocalRuntimeDispatch(
        adapter_command_topic="onex.cmd.codex.pattern-b-dispatch.v1",
        state_root=tmp_path / "state",
    )
    correlation = uuid4()
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="context_gate_cascade_compute",
            requester="synthetic-cascade-probe",
            payload=request_data()
            | {"gate": "jev", "repository_visibility": visibility},
            correlation_id=correlation,
            response_topic=default_response_topic(),
            timeout_seconds=5.0,
        )
    )
    assert evidence.node_name == "node_context_gate_cascade_compute"
    assert evidence.event_bus_backend == "inmemory"
    assert (
        evidence.command_topic == "onex.cmd.omnimarket.context-gate-cascade-evaluate.v1"
    )
    assert (
        evidence.terminal_topic
        == "onex.evt.omnimarket.context-gate-cascade-evaluated.v1"
    )
    assert terminal.correlation_id == correlation
    if visibility == "public":
        assert terminal.payload is not None
        assert terminal.status == "completed"
        assert terminal.payload["recommended"] is True
        assert terminal.payload["cost_saved_usd"] == "0.24"
        assert (
            terminal.payload["gate_training_evidence_id"]
            == "synthetic-training-evidence"
        )
        assert "gate_output" not in terminal.payload
    else:
        assert terminal.status == "failed"
        assert "Jev requires public repository attribution" in terminal.error_message
