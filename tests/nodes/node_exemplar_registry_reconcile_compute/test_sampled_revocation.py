# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TC.5 AC12: count-derived metrics and minimum-sample revocation."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_exemplar_registry_reconcile_compute.models.model_exemplar_registry_reconcile import (
    ModelExemplarRegistryReconcileResult,
)
from tests.nodes.node_exemplar_registry_reconcile_compute.test_golden_chain_exemplar_registry_reconcile_compute import (
    NODE_DIR,
    _run,
    current_row,
    decision,
    reconcile,
    request,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["typed", "inmemory", "cli"])
@pytest.mark.parametrize(
    ("counts", "admitted", "expected", "precision", "recall"),
    [
        ({}, False, "deprecate", None, None),
        ({"false_positive": 1}, False, "deprecate", 0.0, None),
        ({"false_positive": 3}, False, "revoke", 0.0, None),
        ({"true_positive": 9, "false_positive": 1}, True, "retain", 0.9, 1.0),
        ({"true_positive": 1, "false_positive": 3}, True, "revoke", 0.25, 1.0),
        ({"true_positive": 1, "false_negative": 3}, True, "revoke", 1.0, 0.25),
        ({"true_negative": 10}, True, "retain", None, None),
        ({"true_negative": 10, "false_positive": 1}, True, "retain", 0.0, None),
        ({"true_negative": 10, "false_negative": 1}, True, "retain", None, 0.0),
    ],
)
async def test_revocation_uses_counts_on_each_dispatch_binding(
    tmp_path: Path,
    binding: str,
    counts: dict[str, int],
    admitted: bool,
    expected: str,
    precision: float | None,
    recall: float | None,
) -> None:
    candidate = decision(
        "node_a", admitted=admitted, codes=() if admitted else ("gate_failed",)
    )
    payload = request(candidate, rows=[current_row("node_a")])
    payload["evaluation_counts"] = {"node_a": counts}
    if binding == "typed":
        result = reconcile(payload)
    elif binding == "inmemory":
        result = (await _run(tmp_path, payload)).handler_result
    else:
        input_path = tmp_path / "input.json"
        input_path.write_text(json.dumps(payload))
        state = tmp_path / "cli-state"
        completed = subprocess.run(
            [
                str(Path(sys.executable).parent / "onex"),
                "node",
                "node_exemplar_registry_reconcile_compute",
                "--input",
                str(input_path),
                "--state-root",
                str(state),
                "--backend",
                "event_bus=inmemory",
                "--timeout",
                "10",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        saved = json.loads((state / "workflow_result.json").read_text())
        assert saved["handler_locus"] == "in_process"
        result = ModelExemplarRegistryReconcileResult.model_validate(
            saved["handler_result"]
        )
    assert result == reconcile(payload)
    [operation] = result.operations
    assert operation.operation == expected
    assert operation.evaluation.sample_size == sum(counts.values())
    assert operation.evaluation.precision == precision
    assert operation.evaluation.recall == recall
    if expected == "revoke":
        assert operation.lifecycle_after == "revoked"
        assert operation.receipt is None


def test_missing_history_does_not_revoke_a_first_failure() -> None:
    payload = request(
        decision("node_a", admitted=False, codes=("gate_failed",)),
        rows=[current_row("node_a")],
    )
    [operation] = reconcile(payload).operations
    assert operation.operation == "deprecate"
    assert operation.evaluation.sample_size == 0
    assert "insufficient_evaluation_sample" in operation.reason_codes


def test_overlay_policy_controls_minimum_sample_and_thresholds() -> None:
    payload = request(decision("node_a"), rows=[current_row("node_a")])
    payload["evaluation_counts"] = {"node_a": {"true_positive": 1, "false_positive": 3}}
    payload["policy"] = {
        "minimum_sample_size": 5,
        "minimum_precision": 0.8,
        "minimum_recall": 0.8,
    }
    [operation] = reconcile(payload).operations
    assert operation.operation == "retain"
    payload["policy"]["minimum_sample_size"] = 4
    [operation] = reconcile(payload).operations
    assert operation.operation == "revoke"
    assert "precision_below_threshold" in operation.reason_codes
    payload["policy"]["minimum_precision"] = 0.25
    [operation] = reconcile(payload).operations
    assert operation.operation == "retain"


@pytest.mark.parametrize(
    "counts",
    [
        {"false_positive": -1},
        {"true_positive": True},
        {"true_positive": 1.5},
        {"recall": 0.5},
    ],
)
def test_evidence_refuses_negative_noninteger_or_asserted_metrics(
    counts: dict[str, Any],
) -> None:
    payload = request(decision("node_a"), rows=[current_row("node_a")])
    payload["evaluation_counts"] = {"node_a": counts}
    with pytest.raises(ValidationError):
        reconcile(payload)


@pytest.mark.parametrize(
    "policy",
    [{"minimum_sample_size": 1}, {"minimum_precision": 1.1}, {"minimum_recall": -0.1}],
)
def test_invalid_policy_is_refused(policy: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        reconcile(request(decision("node_a"), policy=policy))


def test_evidence_and_policy_bind_the_reconciliation_hash() -> None:
    payload = request(decision("node_a"), rows=[current_row("node_a")])
    payload["evaluation_counts"] = {"node_a": {"true_positive": 4}}
    first = reconcile(payload)
    payload["evaluation_counts"]["node_a"]["true_positive"] = 5
    second = reconcile(payload)
    assert first.reconciliation_hash != second.reconciliation_hash
    payload["policy"] = {"minimum_sample_size": 6}
    assert reconcile(payload).reconciliation_hash != second.reconciliation_hash


def test_contract_overlay_and_model_agree_on_policy() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    overlay = yaml.safe_load((NODE_DIR / "overlay.yaml").read_text())
    declared = dict(contract["configuration"]["removal_policy"])
    declared.pop("model")
    assert declared == overlay["policy"]
    payload = request(decision("node_a"), policy=overlay["policy"])
    assert reconcile(payload).policy.model_dump(mode="json") == declared
