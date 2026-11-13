# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam test (OMN-23635): the overlay document, the budget node's ``input_ceiling`` operation and the routing read.

Three sides share one shape and each is pinned here, so a change on either side turns a test red:

* the overlay document (the overlays repository's ``node_delegation_code_budget_compute/overlay.yaml``, whose own repository
  test pins the same key list spelled below);
* the node's ``input_ceiling`` operation (request fields, result fields, refusal code);
* the routing handler's read (``measured_correct_ceiling``) and the edit loop's refusal text that names the overlay key.

Each test runs in memory: the node is called directly, as the routing handler calls it, and the overlay is the fixture file.
The real-broker reading of the seam is the dev lane's own routing, observed in the PR's runtime proof, because the node is not a bus hop yet.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.routing.measured_correct_ceiling import (
    measured_correct_ceiling,
    refuse_above_measured_correct_ceiling,
)
from omnimarket.models.delegation.wire.model_attempt_rubric_verdict import (
    ModelAttemptRubricVerdict,
)
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.models.model_delegation_code_budget import (
    EnumCodeBudgetRefusal,
    ModelCodeBudgetOverlay,
    ModelCodeBudgetRequest,
    input_ceiling_overlay_key,
)
from omnimarket.nodes.node_delegation_code_budget_compute.handlers.handler_delegation_code_budget import (
    HandlerDelegationCodeBudget,
)

pytestmark = pytest.mark.unit

NAME = "node_delegation_code_budget_compute"
OVERLAY_FILE = (
    Path(__file__).resolve().parent / "fixtures" / "overlays" / NAME / "overlay.yaml"
)

# The overlay document's keys as the overlays repository's own test spells them (expected_keys in tests/test_overlay_roots.py there).
OVERLAYS_REPO_KEYS = {
    "class_budgets_s",
    "sizing_margin",
    "round_up_to_s",
    "min_completed_samples",
    "ceiling_s",
    "input_ceilings",
}


# The one optional key (OMN-24061): a document that omits it still loads, so the overlays repository may add it after this model
# accepts it, and never before (an overlay key no reader accepts stops every consumer of the overlay).
OPTIONAL_OVERLAY_KEYS = {"class_sampling"}


def _overlay_document() -> dict[str, object]:
    return yaml.safe_load(OVERLAY_FILE.read_text(encoding="utf-8"))


def test_overlay_document_keys_match_the_model_and_the_overlays_repository() -> None:
    assert set(ModelCodeBudgetOverlay.model_fields) == (
        OVERLAYS_REPO_KEYS | OPTIONAL_OVERLAY_KEYS
    )
    required = {
        name
        for name, field in ModelCodeBudgetOverlay.model_fields.items()
        if field.is_required()
    }
    assert required == OVERLAYS_REPO_KEYS
    document_keys = set(_overlay_document())
    assert (
        OVERLAYS_REPO_KEYS
        <= document_keys
        <= (OVERLAYS_REPO_KEYS | OPTIONAL_OVERLAY_KEYS)
    )


def test_the_overlay_key_the_refusal_names_is_the_ticket_s_key() -> None:
    assert (
        input_ceiling_overlay_key("local-coder", "code_generation")
        == "delegation.code_budget.input_ceilings.local-coder.code_generation"
    )


def test_the_nodes_result_fields_the_routing_read_uses_are_pinned() -> None:
    request = ModelCodeBudgetRequest(
        operation="input_ceiling",
        task_class="code_generation",
        backend="local-coder",
        overlay=ModelCodeBudgetOverlay.model_validate(_overlay_document()),
    )
    result = HandlerDelegationCodeBudget().handle(request)
    assert result.input_ceiling_tokens == 8169
    assert result.refusal is None
    unnamed = HandlerDelegationCodeBudget().handle(
        request.model_copy(update={"task_class": "no_such_class"})
    )
    assert unnamed.refusal is EnumCodeBudgetRefusal.UNNAMED_INPUT_CEILING
    assert unnamed.refusal.value == "unnamed_input_ceiling"
    assert unnamed.input_ceiling_tokens is None


def test_routing_read_returns_what_the_node_returns_for_the_same_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "roots"
    target = root / NAME / "overlay.yaml"
    target.parent.mkdir(parents=True)
    document = _overlay_document()
    document["input_ceilings"] = {"local-coder": {"code_generation": 1234}}
    target.write_text(yaml.safe_dump(document), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    assert measured_correct_ceiling("local-coder", "code_generation") == 1234
    assert measured_correct_ceiling("local-coder", "test") is None


def test_the_edit_loops_refusal_names_the_overlay_key() -> None:
    decided = refuse_above_measured_correct_ceiling(
        ModelQualityGateResult(correlation_id=uuid4(), passed=True, quality_score=0.9),
        ModelAttemptRubricVerdict(
            rubric_version="v-test",
            task_class="code_generation",
            outcome="UNDETERMINED",
            undetermined_criteria=("passes_existing_tests",),
        ),
        backend_ref="local-coder",
        task_class="code_generation",
        request_text="x" * (8854 * 4),
    )
    assert decided.passed is False
    assert decided.failure_reasons[0].startswith(
        "above_measured_correct_ceiling: input 8854 tokens > local-coder code_generation ceiling 8169"
    )
    assert (
        "overlay key delegation.code_budget.input_ceilings.local-coder.code_generation"
        in decided.failure_reasons[0]
    )
