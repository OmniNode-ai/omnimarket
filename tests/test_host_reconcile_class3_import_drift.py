# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Class 3: stale installed code must be a typed refusal (OMN-20865).

Ledger evidence: ROLLING_WORK_LEDGER.md:61203 (2026-10-10T04:21:16Z,
installed code retained a deleted module), :62645 (2026-10-10T15:09:45Z,
installed watcher missed the merged rotation), and :66118 (2026-10-11T10:58:22Z,
installed landing controller predated the node handover).
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.model_host_reconcile import ModelHostReconcileEvaluateRequest
from omnimarket.nodes.node_host_reconcile_compute.handlers.handler_host_reconcile_compute import (
    HandlerHostReconcileCompute,
)

pytestmark = pytest.mark.unit


def evaluate(
    component: str, installed: str, expected: str
) -> ModelHostReconcileEvaluateRequest:
    return ModelHostReconcileEvaluateRequest.model_validate(
        {
            "mode": "repair",
            "workspace_root": "/workspace",
            "scripts_dir": "/scripts",
            "rerun_command": "reconcile",
            "receipt_path": "/receipt",
            "facts": [
                {
                    "kind": "installed_revision",
                    "surface": "installed-copy",
                    "component_name": component,
                    "installed_revision": installed,
                    "expected_revision": expected,
                }
            ],
        }
    )


@pytest.mark.parametrize("component", ["landing-controller", "plugin", "pr-watcher"])
def test_class3_stale_installed_copy_returns_typed_import_drift(component: str) -> None:
    request = evaluate(component, "older-revision", "claimed-repo-revision")
    result = HandlerHostReconcileCompute().handle(request)
    assert result.exit_code == 2
    assert result.failures == result.dispatch_premise_failures == 1
    assert not result.stamp_floor
    decision = result.surfaces[0]
    assert decision.verdict == "IMPORT_DRIFT"
    assert decision.dispatch_premise
    assert decision.import_drift is not None
    assert decision.import_drift.model_dump(mode="json") == {
        "outcome": "IMPORT_DRIFT",
        "component_name": component,
        "installed_revision": "older-revision",
        "expected_revision": "claimed-repo-revision",
    }
    assert decision.remedy == "reconcile"
    assert component in decision.detail
    assert "older-revision" in decision.detail
    assert "claimed-repo-revision" in decision.detail
    assert result.model_dump(mode="json")["surfaces"][0]["import_drift"] == (
        decision.import_drift.model_dump(mode="json")
    )
    assert result == HandlerHostReconcileCompute().handle(request)


def test_matching_installed_revision_keeps_the_dispatch_floor_eligible() -> None:
    result = HandlerHostReconcileCompute().handle(evaluate("plugin", "rev", "rev"))
    assert result.exit_code == result.failures == result.dispatch_premise_failures == 0
    assert result.stamp_floor
    assert result.surfaces[0].verdict == "ALREADY_AT_TARGET"
    assert result.surfaces[0].import_drift is None
    assert "import_drift" not in result.surfaces[0].model_dump()
    assert result.surfaces[0].remedy == ""


@pytest.mark.parametrize(
    "field", ["component_name", "installed_revision", "expected_revision"]
)
@pytest.mark.parametrize("value", ["", "   ", None])
def test_unread_installed_revision_facts_are_refused(
    field: str, value: str | None
) -> None:
    payload = evaluate("plugin", "rev", "rev").model_dump()
    payload["facts"][0][field] = value
    with pytest.raises(ValidationError):
        ModelHostReconcileEvaluateRequest.model_validate(payload)


def test_import_drift_verdict_uses_the_existing_declared_compute_event() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src/omnimarket/nodes/node_host_reconcile_compute/contract.yaml"
    )
    contract = yaml.safe_load(path.read_text())
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert "IMPORT_DRIFT" in contract["outputs"]["surfaces"]["description"]
