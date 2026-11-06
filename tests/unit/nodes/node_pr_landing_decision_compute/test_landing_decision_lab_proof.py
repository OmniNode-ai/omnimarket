# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A PR citing a factory task ticket merges only on a PASS lab-proof receipt for its head.

The factory tickets (the manifest) and the repository profiles are facts the caller
supplies; the receipt rows are the projection's rows for the PR. The decision holds
no ticket id and no profile id of its own.
"""

from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    HandlerPrLandingDecision,
    decide_landing,
    land_coverage_gaps,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_lab_proof import (
    EnumLandingLabProofReason,
)

OBSERVED = datetime(2026, 10, 29, 1, 0, 0, tzinfo=UTC)
PR = "acme/app#7"
HEAD = f"{7:040x}"
OLD_HEAD = f"{6:040x}"
PROFILE = {
    "repo": "acme/app",
    "profile_id": "app.runtime_image",
    "profile_version": "3",
    "negative_control_kind": "base_rerun",
}
FACTORY_TICKET = "TKT-100"
NODE = "node_pr_landing_decision_compute"
NODE_DIR = Path(__file__).resolve().parents[4] / "src" / "omnimarket" / "nodes" / NODE


def _receipt(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "row_id": f"acme/app#7@{HEAD}:app.runtime_image@3",
        "pr": PR,
        "head_sha": HEAD,
        "profile_id": "app.runtime_image",
        "profile_version": "3",
        "result": "PASS",
        "negative_control_kind": "base_rerun",
        "negative_control_result": "FAIL",
    }
    return {**base, **fields}


def _facts(
    *,
    receipts: list[dict[str, Any]] | None = None,
    cited: list[str] | None = None,
    manifest: list[str] | None = None,
    profiles: list[dict[str, Any]] | None = None,
) -> ModelLandingFacts:
    return ModelLandingFacts.model_validate(
        {
            "tick": 1,
            "observed_at": OBSERVED.isoformat(),
            "prs": [
                {
                    "pr": PR,
                    "head_sha": HEAD,
                    "state": "open",
                    "ci": "green",
                    "created_at": OBSERVED.isoformat(),
                    "cited_tickets": [FACTORY_TICKET] if cited is None else cited,
                }
            ],
            "factory_tickets": [FACTORY_TICKET] if manifest is None else manifest,
            "lab_proof_profiles": [PROFILE] if profiles is None else profiles,
            "lab_proof_receipts": [_receipt()] if receipts is None else receipts,
        }
    )


def _merges(decision: ModelLandingDecision) -> list[str]:
    return [
        a.subject
        for a in (*decision.actions, *decision.observed_actions)
        if a.kind is EnumLandingActionKind.MERGE
    ]


def _holds(decision: ModelLandingDecision) -> dict[str, EnumLandingLabProofReason]:
    return {h.pr: h.reason for h in decision.lab_proof_holds}


@pytest.mark.unit
@pytest.mark.parametrize(
    ("receipts", "reason"),
    [
        pytest.param([], EnumLandingLabProofReason.ABSENT, id="absent"),
        pytest.param(
            [_receipt(result="FAIL")], EnumLandingLabProofReason.FAIL, id="fail"
        ),
        pytest.param(
            [_receipt(result="INCONCLUSIVE")],
            EnumLandingLabProofReason.INCONCLUSIVE,
            id="inconclusive",
        ),
        pytest.param(
            [
                _receipt(
                    head_sha=OLD_HEAD,
                    row_id=f"acme/app#7@{OLD_HEAD}:app.runtime_image@3",
                )
            ],
            EnumLandingLabProofReason.STALE_HEAD,
            id="older-head",
        ),
    ],
)
def test_factory_pr_without_a_passing_receipt_for_its_head_is_not_merged(
    receipts: list[dict[str, Any]], reason: EnumLandingLabProofReason
) -> None:
    facts = _facts(receipts=receipts)
    decision = decide_landing(facts)
    assert _merges(decision) == []
    assert _holds(decision) == {PR: reason}
    assert land_coverage_gaps(facts, decision) == ()


@pytest.mark.unit
def test_factory_pr_with_a_passing_receipt_and_a_failing_control_merges() -> None:
    facts = _facts()
    decision = decide_landing(facts)
    assert _merges(decision) == [PR]
    assert decision.lab_proof_holds == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    "control",
    [
        pytest.param(
            {"negative_control_result": "PASS"}, id="control-recorded-passing"
        ),
        pytest.param(
            {"negative_control_result": None, "negative_control_kind": None},
            id="control-not-recorded",
        ),
        pytest.param(
            {"negative_control_kind": "sabotage_import"}, id="control-of-another-kind"
        ),
    ],
)
def test_a_receipt_whose_negative_control_did_not_fail_does_not_admit_the_merge(
    control: dict[str, Any],
) -> None:
    facts = _facts(receipts=[_receipt(**control)])
    decision = decide_landing(facts)
    assert _merges(decision) == []
    assert _holds(decision) == {PR: EnumLandingLabProofReason.CONTROL_NOT_FAILING}
    assert land_coverage_gaps(facts, decision) == ()


@pytest.mark.unit
def test_a_profile_that_declares_no_control_asks_for_none() -> None:
    profile = {**PROFILE, "negative_control_kind": None}
    receipt = _receipt(negative_control_kind=None, negative_control_result=None)
    decision = decide_landing(_facts(profiles=[profile], receipts=[receipt]))
    assert _merges(decision) == [PR]


@pytest.mark.unit
def test_a_factory_pr_in_a_repository_without_a_profile_is_not_merged() -> None:
    decision = decide_landing(_facts(profiles=[]))
    assert _merges(decision) == []
    assert _holds(decision) == {PR: EnumLandingLabProofReason.PROFILE_MISSING}


@pytest.mark.unit
def test_receipts_of_another_profile_version_or_another_pr_do_not_count() -> None:
    other_version = _receipt(profile_version="2", row_id="other-version")
    other_pr = _receipt(pr="acme/app#8", row_id="other-pr")
    decision = decide_landing(_facts(receipts=[other_version, other_pr]))
    assert _merges(decision) == []
    assert _holds(decision) == {PR: EnumLandingLabProofReason.ABSENT}


@pytest.mark.unit
@pytest.mark.parametrize(
    "receipts",
    [
        pytest.param([], id="no-receipt"),
        pytest.param([_receipt(result="FAIL")], id="failing-receipt"),
    ],
)
def test_a_pr_citing_no_factory_ticket_is_decided_as_today(
    receipts: list[dict[str, Any]],
) -> None:
    facts = _facts(receipts=receipts, cited=["OTHER-5"])
    decision = decide_landing(facts)
    assert _merges(decision) == [PR]
    assert decision.lab_proof_holds == ()


@pytest.mark.unit
def test_facts_without_the_new_fields_decide_as_before() -> None:
    facts = ModelLandingFacts.model_validate(
        {
            "tick": 1,
            "observed_at": OBSERVED.isoformat(),
            "prs": [
                {
                    "pr": PR,
                    "head_sha": HEAD,
                    "state": "open",
                    "ci": "green",
                    "created_at": OBSERVED.isoformat(),
                }
            ],
        }
    )
    decision = decide_landing(facts)
    assert _merges(decision) == [PR]
    assert decision.lab_proof_holds == ()


@pytest.mark.unit
def test_the_same_facts_decide_differently_under_two_manifests() -> None:
    """The ticket set and the profiles are facts: the node holds no id of its own."""
    failing = [_receipt(result="FAIL")]
    under_factory = decide_landing(_facts(receipts=failing, manifest=[FACTORY_TICKET]))
    under_other = decide_landing(_facts(receipts=failing, manifest=["TKT-200"]))
    assert _merges(under_factory) == []
    assert _holds(under_factory) == {PR: EnumLandingLabProofReason.FAIL}
    assert _merges(under_other) == [PR]
    assert under_other.lab_proof_holds == ()


@pytest.mark.unit
def test_the_same_receipt_decides_differently_under_two_profiles() -> None:
    sabotage = {**PROFILE, "negative_control_kind": "sabotage_import"}
    assert _merges(decide_landing(_facts(profiles=[PROFILE]))) == [PR]
    held = decide_landing(_facts(profiles=[sabotage]))
    assert _holds(held) == {PR: EnumLandingLabProofReason.CONTROL_NOT_FAILING}


@pytest.mark.unit
def test_the_lab_proof_sources_hold_no_ticket_or_profile_id() -> None:
    model = (NODE_DIR / "models" / "model_landing_lab_proof.py").read_text(
        encoding="utf-8"
    )
    handler = (NODE_DIR / "handlers" / "handler_pr_landing_decision.py").read_text(
        encoding="utf-8"
    )
    assert re.search(r"[A-Z]+-\d+", model) is None
    for text in (model, handler):
        for profile_id in ("runtime_image", "foundation_override", "sabotage_import"):
            assert profile_id not in text
        assert "base_rerun" not in text


@pytest.mark.unit
def test_direct_and_contract_routed_results_are_byte_identical_json() -> None:
    """In process and through the entry `onex run-node` resolves from the contract."""
    route = _resolve_node_route(NODE)
    input_model = getattr(
        importlib.import_module(route.input_model_module), route.input_model_name
    )
    routed_handler = getattr(
        importlib.import_module(route.handler_module), route.handler_class
    )()
    for facts in (
        _facts(),
        _facts(receipts=[]),
        _facts(receipts=[_receipt(negative_control_result="PASS")]),
    ):
        routed = routed_handler.handle(
            input_model.model_validate_json(facts.model_dump_json())
        )
        direct = HandlerPrLandingDecision().handle(facts)
        assert direct.model_dump_json() == routed.model_dump_json()
    held = HandlerPrLandingDecision().handle(_facts(receipts=[]))
    assert _holds(held) == {PR: EnumLandingLabProofReason.ABSENT}
