# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the sibling-premise read effect and its two neighbours.

Upstream: R2 (``node_dev_merge_redeploy_trigger_effect``, OMN-22478) produces one
receipt per merge on the topic this effect reads. Downstream:
``node_release_sibling_premise_compute`` decides from those receipts. These tests
fail when either neighbour changes what crosses the seam: the receipt topic it
publishes on, the receipt's JSON shape on the wire, or its outcome vocabulary.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args, get_origin
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.delegated_test_loop.lab_run_bus import (
    ProtocolBusMessage,
    event_type_for,
)
from omnimarket.delegated_test_loop.lane_bus import open_lab_run_bus
from omnimarket.lab_work.bus import _bytes
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.handlers.adapter_dev_merge_redeploy_host import (
    load_dev_merge_redeploy_topics,
)
from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    EnumDevMergeMode,
    EnumDevMergeOutcome,
    ModelDevMergeRedeployReceipt,
)
from omnimarket.nodes.node_release_sibling_premise_compute.handlers.handler_sibling_premise import (
    HandlerSiblingPremise,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.enum_sibling_premise import (
    EnumSiblingPremiseState,
)
from omnimarket.nodes.node_release_sibling_premise_compute.models.model_sibling_premise_request import (
    ModelSiblingPremiseRequest,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.adapter_receipt_source import (
    decode_receipt,
)
from omnimarket.nodes.node_release_sibling_premise_effect.handlers.lane_keywords import (
    lane_keywords,
)
from omnimarket.nodes.node_release_sibling_premise_effect.models.model_sibling_premise_read_topics import (
    load_topics,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
EFFECT_CONTRACT = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_release_sibling_premise_effect"
    / "contract.yaml"
)
R2_CONTRACT = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_dev_merge_redeploy_trigger_effect"
    / "contract.yaml"
)

REPO = "OmniNode-ai/omnimarket"
HOST = "h053"
CANDIDATE_SHA = "a" * 40
LANE_REVISION = "b" * 40
MERGED_AT = datetime(2026, 1, 1, tzinfo=UTC)
OBSERVED_AT = datetime(2026, 1, 1, 1, tzinfo=UTC)

#: The outcomes the compute node gives a state of their own. Every other
#: non-verified outcome must read NOT_EXERCISED: an outcome the release train
#: has no word for is not evidence the lane is right.
DEDICATED_STATES: dict[EnumDevMergeOutcome, EnumSiblingPremiseState] = {
    EnumDevMergeOutcome.LANE_BEHIND: EnumSiblingPremiseState.BEHIND,
    EnumDevMergeOutcome.DEPLOY_NOT_DONE: EnumSiblingPremiseState.DEPLOY_NOT_DONE,
    EnumDevMergeOutcome.DEADLINE_MISSED: EnumSiblingPremiseState.DEADLINE_MISSED,
    EnumDevMergeOutcome.LANE_UNREADABLE: EnumSiblingPremiseState.LANE_UNREADABLE,
}


def _dispatch(contract: Path) -> dict[str, object]:
    dispatch = yaml.safe_load(contract.read_text())["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    return dispatch


def _receipt(
    *,
    outcome: EnumDevMergeOutcome,
    merge_sha: str = CANDIDATE_SHA,
    correlation_id: UUID | None = None,
    lane_revision: str = "",
) -> ModelDevMergeRedeployReceipt:
    return ModelDevMergeRedeployReceipt(
        host=HOST,
        repo=REPO,
        pr_number=1,
        merge_sha=merge_sha,
        base="dev",
        correlation_id=correlation_id or uuid4(),
        mode=EnumDevMergeMode.OBSERVE,
        outcome=outcome,
        command_published=False,
        runtime_paths=1,
        lane_revision=lane_revision,
        message=f"{outcome.value} happened",
        observed_at=OBSERVED_AT,
    )


def _request(
    receipts: tuple[ModelDevMergeRedeployReceipt, ...],
) -> ModelSiblingPremiseRequest:
    return ModelSiblingPremiseRequest(
        repo=REPO,
        candidate_sha=CANDIDATE_SHA,
        candidate_merged_at=MERGED_AT,
        descendant_shas=(),
        receipts=receipts,
        as_of=MERGED_AT + timedelta(hours=1),
        pending_window_seconds=3600,
    )


async def _wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def test_receipt_topic_is_the_r2_success_terminal() -> None:
    effect = _dispatch(EFFECT_CONTRACT)
    r2 = _dispatch(R2_CONTRACT)
    assert effect["receipt_topic"] == r2["terminal_events"]["success"]
    assert load_topics().receipt == effect["receipt_topic"]
    assert load_dev_merge_redeploy_topics().receipt == effect["receipt_topic"]


async def test_r2_receipt_envelope_round_trips_on_the_inmemory_bus() -> None:
    topic = load_topics().receipt
    delivered: list[bytes] = []

    async def record(message: ProtocolBusMessage) -> None:
        delivered.append(message.value)

    async with open_lab_run_bus(
        **lane_keywords(open_lab_run_bus, "inmemory", None, None, None)
    ) as bus:
        await bus.subscribe(
            topic, on_message=record, group_id="sibling-premise-seam-test"
        )
        receipt = _receipt(
            outcome=EnumDevMergeOutcome.VERIFIED, lane_revision=LANE_REVISION
        )
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=receipt.model_dump(mode="json"),
            event_type=event_type_for(topic),
            correlation_id=receipt.correlation_id,
        )
        await bus.publish(
            topic, str(receipt.correlation_id).encode("utf-8"), _bytes(envelope)
        )

    await _wait_for(lambda: len(delivered) == 1)
    assert len(delivered) == 1
    assert decode_receipt(delivered[0]) == receipt


def test_renamed_lane_revision_key_decodes_to_no_receipt() -> None:
    receipt = _receipt(
        outcome=EnumDevMergeOutcome.VERIFIED, lane_revision=LANE_REVISION
    )
    payload: dict[str, object] = dict(receipt.model_dump(mode="json"))
    payload["lane_revision_seen"] = payload.pop("lane_revision")
    envelope = ModelEventEnvelope[dict[str, object]](
        payload=payload,
        event_type=event_type_for(load_topics().receipt),
        correlation_id=receipt.correlation_id,
    )
    assert decode_receipt(_bytes(envelope)) is None


def test_only_a_verified_outcome_reads_ready() -> None:
    for outcome in EnumDevMergeOutcome:
        if outcome is EnumDevMergeOutcome.VERIFIED:
            continue
        verdict = HandlerSiblingPremise().handle(_request((_receipt(outcome=outcome),)))
        assert verdict.state is not EnumSiblingPremiseState.READY, outcome.value
    ready = HandlerSiblingPremise().handle(
        _request(
            (
                _receipt(
                    outcome=EnumDevMergeOutcome.VERIFIED, lane_revision=LANE_REVISION
                ),
            )
        )
    )
    assert ready.state is EnumSiblingPremiseState.READY
    assert ready.ready is True


def test_request_receipts_are_typed_as_r2_receipts() -> None:
    annotation = ModelSiblingPremiseRequest.model_fields["receipts"].annotation
    assert get_origin(annotation) is tuple
    assert ModelDevMergeRedeployReceipt in get_args(annotation)


def test_every_r2_outcome_is_decided_by_the_compute_handler() -> None:
    for outcome in EnumDevMergeOutcome:
        if outcome is EnumDevMergeOutcome.VERIFIED:
            # Its verdict is pinned by test_only_a_verified_outcome_reads_ready.
            continue
        verdict = HandlerSiblingPremise().handle(_request((_receipt(outcome=outcome),)))
        assert verdict.state is DEDICATED_STATES.get(
            outcome, EnumSiblingPremiseState.NOT_EXERCISED
        ), outcome.value


async def test_topic_reader_authenticates_with_the_bound_lane_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The group-less topic reader builds its credentials for ITS broker (the bound lane transport).

    The release read opens the lane's bus first, which binds the lane transport; a reader that built
    its credentials from the environment alone could not authenticate to a SASL lane (observed on
    the dev lane: connection closed). The control is the reader before this change, which never
    asked for a broker-specific answer.
    """
    import aiokafka
    from omnibase_infra.event_bus import kafka_auth

    from omnimarket.nodes.node_topic_archive_effect.handlers._kafka_topic_reader import (
        AiokafkaTopicReader,
    )

    asked: list[str] = []

    def fake_for(bootstrap: str) -> dict[str, object]:
        asked.append(bootstrap)
        return {}

    class StubConsumer:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(kafka_auth, "build_aiokafka_auth_kwargs_for", fake_for)
    monkeypatch.setattr(aiokafka, "AIOKafkaConsumer", StubConsumer)
    reader = AiokafkaTopicReader("broker.example:9092")
    await reader._client()
    await reader.close()
    assert asked == ["broker.example:9092"]
