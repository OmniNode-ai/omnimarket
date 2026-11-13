# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the release sibling premise compute node."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeMode,
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers import (
    handler_sibling_premise,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.enum_sibling_premise import (
    EnumSiblingPremiseState,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.errors import (
    SiblingPremiseInputError,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_request import (
    ModelSiblingPremiseRequest,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_verdict import (
    ModelSiblingPremiseVerdict,
)

pytestmark = pytest.mark.unit

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_release_sibling_premise_compute"
    / "contract.yaml"
)

HOST = "h053"
REPO = "OmniNode-ai/omnimarket"
BASE = "dev"


def _receipt(
    *,
    merge_sha: str,
    outcome: EnumDevMergeOutcome,
    observed_at: datetime,
    correlation_id: UUID,
    repo: str = REPO,
    lane_revision: str = "",
    message: str = "",
) -> ModelDevMergeRedeployReceipt:
    return ModelDevMergeRedeployReceipt(
        host=HOST,
        repo=repo,
        pr_number=1,
        merge_sha=merge_sha,
        base=BASE,
        correlation_id=correlation_id,
        mode=EnumDevMergeMode.OBSERVE,
        outcome=outcome,
        command_published=False,
        runtime_paths=1,
        lane_revision=lane_revision,
        message=message,
        observed_at=observed_at,
    )


def _request(
    *,
    receipts: tuple[ModelDevMergeRedeployReceipt, ...] = (),
    candidate_sha: str = "a" * 40,
    descendant_shas: tuple[str, ...] = (),
    candidate_merged_at: datetime | None = None,
    as_of: datetime | None = None,
    pending_window_seconds: int = 3600,
    repo: str = REPO,
) -> ModelSiblingPremiseRequest:
    merged_at = candidate_merged_at or datetime(2026, 1, 1, tzinfo=UTC)
    return ModelSiblingPremiseRequest(
        repo=repo,
        candidate_sha=candidate_sha,
        candidate_merged_at=merged_at,
        descendant_shas=descendant_shas,
        receipts=receipts,
        as_of=as_of or merged_at + timedelta(hours=1),
        pending_window_seconds=pending_window_seconds,
    )


def test_golden_verified_candidate_is_ready() -> None:
    cid = uuid4()
    receipt = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid,
        lane_revision="b" * 40,
    )
    verdict = HandlerSiblingPremise().handle(_request(receipts=(receipt,)))
    assert verdict.state is EnumSiblingPremiseState.READY
    assert verdict.covering_merge_sha == "a" * 40
    assert verdict.lane_revision == "b" * 40
    assert verdict.correlation_id == cid
    assert verdict.receipts_considered == 1
    assert verdict.ready is True


def test_incident_folded_rebuild_is_ready_via_descendant() -> None:
    cid_behind = uuid4()
    cid_verified = uuid4()
    behind = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.LANE_BEHIND,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid_behind,
    )
    verified = _receipt(
        merge_sha="c" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
        correlation_id=cid_verified,
        lane_revision="d" * 40,
    )
    verdict = HandlerSiblingPremise().handle(
        _request(
            receipts=(behind, verified),
            descendant_shas=("c" * 40,),
        )
    )
    assert verdict.state is EnumSiblingPremiseState.READY
    assert verdict.covering_merge_sha == "c" * 40
    assert verdict.lane_revision == "d" * 40


def test_empty_receipts_pending_inside_window() -> None:
    merged_at = datetime(2026, 1, 1, 0, tzinfo=UTC)
    verdict = HandlerSiblingPremise().handle(
        _request(
            candidate_merged_at=merged_at,
            as_of=merged_at + timedelta(seconds=60),
            pending_window_seconds=3600,
        )
    )
    assert verdict.state is EnumSiblingPremiseState.PENDING
    assert verdict.receipts_considered == 0


def test_empty_receipts_absent_outside_window() -> None:
    merged_at = datetime(2026, 1, 1, 0, tzinfo=UTC)
    verdict = HandlerSiblingPremise().handle(
        _request(
            candidate_merged_at=merged_at,
            as_of=merged_at + timedelta(seconds=7200),
            pending_window_seconds=3600,
        )
    )
    assert verdict.state is EnumSiblingPremiseState.ABSENT
    assert verdict.receipts_considered == 0


def test_non_verified_outcome_mapping() -> None:
    expected: dict[EnumDevMergeOutcome, EnumSiblingPremiseState] = {
        EnumDevMergeOutcome.DECLINED_REPO: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.DECLINED_BASE: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.DECLINED_NOT_RUNTIME: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.FAILED_FILES_UNREADABLE: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.FAILED_CLASSIFIER: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.FAILED_BUILD_CONTEXT_REF: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.FAILED_PUBLISH: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.FAILED_INTERNAL: EnumSiblingPremiseState.NOT_EXERCISED,
        EnumDevMergeOutcome.DEPLOY_NOT_DONE: EnumSiblingPremiseState.DEPLOY_NOT_DONE,
        EnumDevMergeOutcome.LANE_BEHIND: EnumSiblingPremiseState.BEHIND,
        EnumDevMergeOutcome.LANE_UNREADABLE: EnumSiblingPremiseState.LANE_UNREADABLE,
        EnumDevMergeOutcome.DEADLINE_MISSED: EnumSiblingPremiseState.DEADLINE_MISSED,
    }
    for outcome, want in expected.items():
        receipt = _receipt(
            merge_sha="a" * 40,
            outcome=outcome,
            observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
            correlation_id=uuid4(),
            message=f"{outcome.value} happened",
        )
        verdict = HandlerSiblingPremise().handle(_request(receipts=(receipt,)))
        assert verdict.state is want, f"{outcome.value} -> {verdict.state}"
        assert verdict.receipts_considered == 1


def test_unrelated_receipts_are_ignored() -> None:
    cid = uuid4()
    receipt = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid,
        repo="other/repo",
    )
    merged_at = datetime(2026, 1, 1, 0, tzinfo=UTC)
    verdict = HandlerSiblingPremise().handle(
        _request(
            receipts=(receipt,),
            candidate_merged_at=merged_at,
            as_of=merged_at + timedelta(seconds=60),
            pending_window_seconds=3600,
        )
    )
    assert verdict.state is EnumSiblingPremiseState.PENDING
    assert verdict.receipts_considered == 0


def test_order_independence() -> None:
    cid1 = uuid4()
    cid2 = uuid4()
    r1 = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.LANE_BEHIND,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid1,
    )
    r2 = _receipt(
        merge_sha="c" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
        correlation_id=cid2,
        lane_revision="d" * 40,
    )
    req = _request(receipts=(r1, r2), descendant_shas=("c" * 40,))
    req_rev = _request(receipts=(r2, r1), descendant_shas=("c" * 40,))
    v1 = HandlerSiblingPremise().handle(req)
    v2 = HandlerSiblingPremise().handle(req_rev)
    assert v1 == v2


def test_candidate_merged_after_as_of_raises() -> None:
    merged_at = datetime(2026, 1, 1, 2, tzinfo=UTC)
    with pytest.raises(SiblingPremiseInputError):
        HandlerSiblingPremise().handle(
            _request(
                candidate_merged_at=merged_at,
                as_of=merged_at - timedelta(seconds=1),
            )
        )


def test_short_candidate_sha_rejected() -> None:
    with pytest.raises(ValidationError):
        _request(candidate_sha="a" * 39)


def test_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ModelSiblingPremiseRequest(
            repo=REPO,
            candidate_sha="a" * 40,
            candidate_merged_at=datetime(2026, 1, 1, tzinfo=UTC),
            receipts=(),
            as_of=datetime(2026, 1, 1, 1, tzinfo=UTC),
            pending_window_seconds=1,
            bogus=1,  # type: ignore[call-arg]
        )


def test_injected_break_flips_incident_to_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid_behind = uuid4()
    cid_verified = uuid4()
    behind = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.LANE_BEHIND,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid_behind,
    )
    verified = _receipt(
        merge_sha="c" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
        correlation_id=cid_verified,
        lane_revision="d" * 40,
    )
    req = _request(receipts=(behind, verified), descendant_shas=("c" * 40,))
    assert HandlerSiblingPremise().handle(req).state is EnumSiblingPremiseState.READY

    monkeypatch.setattr(
        handler_sibling_premise,
        "covering_shas",
        lambda r: frozenset([r.candidate_sha]),
    )
    broken = HandlerSiblingPremise().handle(req)
    assert broken.state is EnumSiblingPremiseState.BEHIND
    assert broken.covering_merge_sha == "a" * 40


def test_contract_shape() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    success = contract["runtime_dispatch"]["terminal_events"]["success"]
    failure = contract["runtime_dispatch"]["terminal_events"]["failure"]
    assert success != failure
    handlers = contract["handler_routing"]["handlers"]
    assert any(h["operation"] == "decide_sibling_premise" for h in handlers)

    handler_module = importlib.import_module(contract["handler"]["module"])
    handler_cls = getattr(handler_module, contract["handler"]["class"])
    assert hasattr(handler_cls, "handle")

    input_module = importlib.import_module(contract["input_model"]["module"])
    assert hasattr(input_module, contract["input_model"]["name"])
    output_module = importlib.import_module(contract["output_model"]["module"])
    assert hasattr(output_module, contract["output_model"]["name"])


def test_golden_chain_event_and_error_chain_event() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    success_topic = contract["runtime_dispatch"]["terminal_events"]["success"]
    failure_topic = contract["runtime_dispatch"]["terminal_events"]["failure"]
    command_topic = contract["runtime_dispatch"]["command_topic"]
    assert command_topic == contract["event_bus"]["subscribe_topics"][0]

    handler_module = importlib.import_module(contract["handler"]["module"])
    handler_cls = getattr(handler_module, contract["handler"]["class"])
    input_module = importlib.import_module(contract["input_model"]["module"])
    input_cls = getattr(input_module, contract["input_model"]["name"])
    output_module = importlib.import_module(contract["output_model"]["module"])
    output_cls = getattr(output_module, contract["output_model"]["name"])

    def dispatch(topic: str, payload: dict) -> tuple[str, object]:
        assert topic == command_topic
        request = input_cls.model_validate(payload)
        try:
            verdict = handler_cls().handle(request)
        except SiblingPremiseInputError:
            return failure_topic, None
        return success_topic, verdict

    cid = uuid4()
    receipt = _receipt(
        merge_sha="a" * 40,
        outcome=EnumDevMergeOutcome.VERIFIED,
        observed_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        correlation_id=cid,
        lane_revision="b" * 40,
    )
    ok_payload = _request(receipts=(receipt,)).model_dump(mode="json")
    topic, out = dispatch(command_topic, ok_payload)
    assert topic == success_topic
    assert isinstance(out, ModelSiblingPremiseVerdict)
    assert out == output_cls.model_validate(out.model_dump(mode="json"))

    bad_payload = _request(
        candidate_merged_at=datetime(2026, 1, 1, 2, tzinfo=UTC),
        as_of=datetime(2026, 1, 1, 1, tzinfo=UTC),
    ).model_dump(mode="json")
    topic, out = dispatch(command_topic, bad_payload)
    assert topic == failure_topic
    assert out is None
