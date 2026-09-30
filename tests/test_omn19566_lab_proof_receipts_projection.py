# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure lab_proof_receipts fold (OMN-19566, T2 slice 2)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_projection_lab_proof_receipts import (
    HandlerProjectionLabProofReceipts,
)
from omnimarket.nodes.node_projection_lab_proof_receipts.models import (
    EnumLabProofReceiptResult,
    ModelLabProofReceiptEvent,
    ModelLabProofReceiptProjectionRequest,
    receipt_key_text,
)

pytestmark = pytest.mark.unit

HEAD = "a" * 40
T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
REPO = "OmniNode-ai/omnibase_infra"
PROFILE = "omnibase_infra.runtime_and_scripts"


def event(
    *,
    result: str = "PASS",
    token: str = "ACCEPTED",
    finished: datetime = T0 + timedelta(minutes=20),
    runner: str = "prove-lane@lab-101",
    verifier: str = "prepr_runtime_pool.judge@mac",
) -> dict[str, Any]:
    """Shaped exactly as omnibase_infra build_pr_head_bus_event writes it."""
    failing = [] if result == "PASS" else ["runtime_main_healthy"]
    return {
        "schema_version": "1.0.0",
        "event_type": "lab-proof-receipt",
        "topic": "onex.evt.omnibase-infra.lab-proof-receipt.v1",  # onex-topic-allow: the producer's own field, fold input data
        "lane": "pr-head",
        "receipt_key": receipt_key_text(REPO, 4217, HEAD, PROFILE, "1"),
        "repo": REPO,
        "pr_number": 4217,
        "head_sha": HEAD,
        "profile_id": PROFILE,
        "profile_version": "1",
        "handler_kind": "runtime_image",
        "result": result,
        "verifier_token": token,
        "verifier_reason": "accepted" if token == "ACCEPTED" else token.lower(),
        "mandatory_checks": ["focused_tests", "runtime_main_healthy"],
        "missing_mandatory_checks": [],
        "failing_checks": failing,
        "started_at": T0.isoformat(),
        "finished_at": finished.isoformat(),
        "runner_identity": runner,
        "verifier_identity": verifier,
        "host": "lab-101",
        "slot": "isolated omnibase-infra-local",
        "carried_from": "",
        "receipt": {"sha": HEAD, "lane": "pr-head", "result": result, "checks": []},
    }


def fold(raw: dict[str, Any], previous: Any = None) -> tuple[bool, Any]:
    result = HandlerProjectionLabProofReceipts().handle(
        ModelLabProofReceiptProjectionRequest(
            event=ModelLabProofReceiptEvent.model_validate(raw), previous_row=previous
        )
    )
    return result.applied, result.row


def test_a_pass_receipt_is_one_row_under_its_key() -> None:
    applied, row = fold(event())
    assert applied
    assert row.receipt_key == f"{REPO}#4217@{HEAD}:{PROFILE}@1"
    assert row.result is EnumLabProofReceiptResult.PASS
    assert row.verifier_token == "ACCEPTED"


def test_a_fail_receipt_is_a_row_like_a_pass() -> None:
    applied, row = fold(event(result="FAIL", token="RESULT_NOT_PASS"))
    assert applied
    assert row.result is EnumLabProofReceiptResult.FAIL
    assert row.failing_checks == ("runtime_main_healthy",)
    assert row.verifier_token == "RESULT_NOT_PASS"


def test_a_redelivery_or_older_proof_never_replaces_a_newer_one() -> None:
    _, stored = fold(event())
    applied, row = fold(event(), previous=stored)
    assert not applied
    assert row == stored
    older = event(result="FAIL", finished=T0 + timedelta(minutes=10))
    applied, row = fold(older, previous=stored)
    assert not applied
    assert row.result is EnumLabProofReceiptResult.PASS


def test_a_newer_proof_of_the_same_key_replaces_it() -> None:
    _, stored = fold(event(result="FAIL", token="RESULT_NOT_PASS"))
    applied, row = fold(event(finished=T0 + timedelta(hours=1)), previous=stored)
    assert applied
    assert row.result is EnumLabProofReceiptResult.PASS


def test_a_key_that_disagrees_with_its_fields_is_refused() -> None:
    raw = event()
    raw["receipt_key"] = receipt_key_text(REPO, 4217, "b" * 40, PROFILE, "1")
    with pytest.raises(ValidationError, match="does not match its own fields"):
        ModelLabProofReceiptEvent.model_validate(raw)


def test_an_embedded_receipt_for_another_head_is_refused() -> None:
    raw = copy.deepcopy(event())
    raw["receipt"]["sha"] = "b" * 40
    with pytest.raises(ValidationError, match="another head"):
        ModelLabProofReceiptEvent.model_validate(raw)


def test_a_post_merge_lane_is_never_a_pr_head_row() -> None:
    raw = event()
    raw["lane"] = "compose-dev"
    with pytest.raises(ValidationError):
        ModelLabProofReceiptEvent.model_validate(raw)


def test_a_runner_that_is_its_own_verifier_is_recorded_with_its_token() -> None:
    applied, row = fold(
        event(token="RUNNER_IS_VERIFIER", runner="same@lab", verifier="same@lab")
    )
    assert applied
    assert row.verifier_token == "RUNNER_IS_VERIFIER"


def test_the_fold_is_deterministic() -> None:
    assert fold(event()) == fold(event())
