# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_receipt_gate_pin_friction: the reducer handler end to end [OMN-20134].

The handler takes the prior row and one observation as JSON, as the runtime hands
them, and returns the fold result first and one friction event per newly flagged
reason after it; the contract routes each class to its topic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinKind,
    EnumPinPrState,
    ModelPinFrictionEvent,
    ModelPinFrictionFoldRequest,
    ModelPinFrictionFoldResult,
    ModelPinFrictionRow,
    ModelPinOutcomeEvent,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.handlers.handler_pin_friction_reducer import (
    HandlerReceiptGatePinFrictionReducer,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_receipt_gate_pin_friction"
    / "contract.yaml"
)
REPO = "OmniNode-ai/omnibase_infra"
TAG = "dev-20261030152335-66d35d2283c3"
BRANCH = f"pin-advance/receipt-gate/{TAG}"
PUSHED = datetime(2026, 10, 31, 1, 0, 0, tzinfo=UTC)
OPENED = PUSHED + timedelta(seconds=90)
HEAD0 = "a" * 40
HEAD1 = "b" * 40


def _request(
    prior: ModelPinFrictionRow | None, observation: dict[str, object]
) -> ModelPinFrictionFoldRequest:
    """The request as the runtime builds it: JSON in, validated model out."""
    return ModelPinFrictionFoldRequest.model_validate(
        {
            "prior_row": None if prior is None else prior.model_dump(mode="json"),
            "observation": observation,
        }
    )


def _outcome(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": "outcome",
        "outcome": "opened",
        "pin_kind": "sha",
        "repository": REPO,
        "core_tag": TAG,
        "branch": BRANCH,
        "branch_head_sha": HEAD0,
        "pr_number": 5477,
        "tag_pushed_at": PUSHED.isoformat(),
        "outcome_at": OPENED.isoformat(),
        "seconds_tag_to_pr_opened": 90.0,
    }
    return {**base, **fields}


def _head(sha: str, at: datetime) -> dict[str, object]:
    return {
        "kind": "head",
        "repo": REPO,
        "head_ref": BRANCH,
        "pr_number": 5477,
        "as_of": at.isoformat(),
        "head_sha": sha,
    }


def _apply(
    prior: ModelPinFrictionRow | None, observation: dict[str, object]
) -> ModelPinFrictionFoldResult:
    return HandlerReceiptGatePinFrictionReducer().handle(_request(prior, observation))


def test_an_outcome_creates_the_row_and_a_fix_push_flags_friction_once() -> None:
    opened = _apply(None, _outcome())
    assert opened.friction_event is None
    assert opened.changed
    assert opened.row is not None
    assert opened.row.pin_kind is EnumPinKind.SHA
    assert opened.row.state is EnumPinPrState.OPENED

    pushed = _apply(opened.row, _head(HEAD1, OPENED + timedelta(minutes=3)))
    assert pushed.row is not None
    assert pushed.row.fix_pushes == 1
    assert isinstance(pushed.friction_event, ModelPinFrictionEvent)
    assert pushed.friction_event.reason is EnumPinFrictionReason.HUMAN_TOUCHED

    again = _apply(pushed.row, _head(HEAD1, OPENED + timedelta(minutes=4)))
    assert not again.changed
    assert again.friction_event is None


def test_a_failed_updater_run_flags_friction_with_no_prior_row() -> None:
    failed = _apply(
        None,
        _outcome(
            outcome="failed",
            branch_head_sha=None,
            pr_number=None,
            seconds_tag_to_pr_opened=None,
            refusal_class="guard",
            detail="pr_create was refused: quota_floor",
        ),
    )
    assert failed.row is not None
    assert failed.row.state is EnumPinPrState.FAILED
    assert failed.friction_event is not None
    assert failed.friction_event.reason is EnumPinFrictionReason.UPDATER_FAILED


def test_an_observation_naming_no_known_branch_changes_nothing() -> None:
    result = _apply(
        None,
        {
            "kind": "merged",
            "repo": REPO,
            "branch": "feature/unrelated",
            "pr_number": 12,
            "merged_at": OPENED.isoformat(),
        },
    )
    assert result.row is None
    assert not result.changed
    assert result.friction_event is None


def test_the_updaters_outcome_event_alone_is_enough_to_flag_a_failed_run() -> None:
    handler = HandlerReceiptGatePinFrictionReducer()
    failed = ModelPinOutcomeEvent.model_validate(
        _outcome(
            outcome="refused",
            branch_head_sha=None,
            pr_number=None,
            seconds_tag_to_pr_opened=None,
            refusal_class="unmatched",
            detail="pin_site_unmatched: no pin line",
        )
    )
    opened = ModelPinOutcomeEvent.model_validate(_outcome())
    flagged = handler.handle(failed)
    assert flagged.friction_event is not None
    assert flagged.friction_event.reason is EnumPinFrictionReason.UPDATER_FAILED
    # a redelivered outcome folds to the same event, so the ledger request id repeats
    assert handler.handle(failed).friction_event == flagged.friction_event
    assert handler.handle(opened).friction_event is None


def test_an_observation_of_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError, match="kind"):
        _request(None, {"kind": "deploy", "repo": REPO})


def test_the_contract_publishes_the_fold_result_and_the_ledger_effect_listens() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    assert contract["node_type"] == "REDUCER_GENERIC"
    assert {e["event_type"]: e["topic"] for e in contract["published_events"]} == {
        "PinFrictionFoldResult": "onex.evt.omnimarket.pin-friction-folded.v1",
    }
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.pin-friction-folded.v1"
    ]
    assert contract["terminal_event"] == "onex.evt.omnimarket.pin-friction-folded.v1"
    assert {
        h["event_model"]["name"]: h["topic"]
        for h in contract["handler_routing"]["handlers"]
    } == {
        "ModelPinFrictionFoldRequest": "onex.cmd.omnimarket.pin-friction-fold-requested.v1",
        "ModelPinOutcomeEvent": "onex.evt.omnimarket.receipt-gate-pin-outcome.v1",
    }
    effect = yaml.safe_load(
        (
            CONTRACT.parent.parent
            / "node_receipt_gate_pin_advance_effect"
            / "contract.yaml"
        ).read_text()
    )
    assert (
        "onex.evt.omnimarket.receipt-gate-pin-outcome.v1"
        in (effect["event_bus"]["publish_topics"])
    )
    assert (
        "onex.evt.omnimarket.receipt-gate-pin-outcome.v1"
        in (contract["event_bus"]["subscribe_topics"])
    )
    assert contract["runtime_dispatch"]["command_topic"] == (
        "onex.cmd.omnimarket.pin-friction-fold-requested.v1"
    )
    ledger = yaml.safe_load(
        (
            CONTRACT.parent.parent
            / "node_receipt_gate_pin_friction_ledger_effect"
            / "contract.yaml"
        ).read_text()
    )
    assert (
        ledger["event_bus"]["subscribe_topics"]
        == contract["event_bus"]["publish_topics"]
    )
