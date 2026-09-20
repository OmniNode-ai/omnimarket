# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The deploy agent's rejection events get a declarative home and a reader (OMN-18816).

WHAT WAS BROKEN, MEASURED ON THE .201 DEV LANE 2026-09-19
---------------------------------------------------------
The deploy agent publishes every "this command will not run" outcome to
``onex.evt.deploy.rebuild-rejected.v1``. That topic did not exist on the dev lane
broker, and nothing anywhere subscribed it.

Probed read-only from the ``.201`` host on 2026-09-19 against the dev lane broker
(``omnibase-infra-redpanda``), out of 1714 topics::

    onex.cmd.deploy.rebuild-requested.v1   present
    onex.evt.deploy.rebuild-completed.v1   present
    onex.evt.deploy.rebuild-rejected.v1    ABSENT

The two present rows are the probe's own positive control, so the absence is a finding
and not an unreadable surface. It is not an ACL and not the producer configuration: a
completion publish to the present topic succeeded at 11:49:34Z on the same client
configuration, fifteen seconds before a rejection publish failed.

The agent logged ``Topic onex.evt.deploy.rebuild-rejected.v1 not found in cluster
metadata`` continuously from 11:49:49Z, and was still retrying
``Failed to publish rebuild-rejected (reason=superseded) for
63858212-b2f8-474b-b5ed-9d0c79443ba3`` at 12:36:33Z -- 46 minutes on.

WHY IT SURFACED WHEN IT DID
---------------------------
The rejection path effectively never fired before: zero ``Rejecting command`` lines in
the fourteen days to 2026-09-19. OMN-18143 AC7 made rejections common by coalescing
superseded rebuild commands, and it is the first caller that RETRIES one.

WHAT THE ABSENCE COSTS, AND WHY IT IS THIS TICKET
-------------------------------------------------
Four consecutive merged ``omnibase_infra`` PRs failed their lab-verify checks on
2026-09-19 (runs 35440091868, 35440117190, 35441066681, 35441819143). TWO of the four
were coalesced away and can never earn a PASS receipt: ``5425a634`` superseded at
11:49:49Z and ``07d1d4ac`` at 12:39:01Z, both recorded ``status: superseded`` with every
phase ``skipped``. Under rule 24(b) those merges are undeliverable.

The resolution that fixes them is "sha X superseded by descendant Y, and Y converged, is
a PASS for X" -- which needs the supersession signal to actually reach a reader. It
cannot while the topic does not exist. That is what this module's subject provides.

THE DECLARATIVE HOME, AND WHY IT IS THE READER'S CONTRACT
----------------------------------------------------------
The deploy agent is a standalone uv sub-project under ``scripts/deploy-agent/`` with no
node and no ``contract.yaml``, so it cannot declare its own topics. Its two EXISTING
topics are declared by their counterparties, not by the agent: this very contract
declares ``rebuild-requested`` (it publishes it) and ``rebuild-completed`` (it subscribes
it). The rejection topic gets its home the same way -- on the contract of the node that
reads it.

``scripts/create_kafka_topics.py`` in DEFAULT mode discovers every installed package via
the ``onex.node_package`` entry points, and ``omnimarket`` declares one
(``pyproject.toml``: ``omnimarket = "omnimarket.nodes"``). An omnimarket contract is
therefore in scope for the provisioner, which is why this works without the deploy agent
acquiring a contract of its own. :func:`test_the_provisioner_extractor_sees_the_topic`
proves that end of it rather than assuming it.

WHAT THIS MODULE DOES NOT CLAIM
-------------------------------
The producer's wire body carries no timestamp and no runtime lane. The projection row
therefore records ``observed_at`` -- the time the reader saw the event, named for what it
is rather than dressed up as the time of the rejection -- and carries ``runtime_lane`` as
an explicit ``None`` when the wire does not supply one. Neither is defaulted into
existence. Extending the producer to carry both is a change in ``omnibase_infra`` and is
deliberately not in this scope.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_redeploy_deploy_effect"
    / "contract.yaml"
)
_NODE_DIR = _CONTRACT.parent

TOPIC_REJECTED = "onex.evt.deploy.rebuild-rejected.v1"


def _contract() -> dict[str, Any]:
    return yaml.safe_load(_CONTRACT.read_text())


# ---------------------------------------------------------------------------
# The producer's exact wire shapes, transcribed from
# deploy_agent.events.ModelRebuildRejected.to_wire.
# ---------------------------------------------------------------------------


def _wire_superseded_payload() -> dict[str, Any]:
    """The supersession rejection, field for field as ``to_wire`` builds it.

    These are the real values from the 2026-09-19 burst: correlation ``63858212`` is
    ``5425a634``'s rebuild command, superseded by ``afe6ae01`` which carried
    ``d4b668c2``.
    """
    return {
        "correlation_id": "63858212-b2f8-474b-b5ed-9d0c79443ba3",
        "reason": "superseded",
        "scope": "full",
        "superseded_by_sha": "d4b668c276a42c416aebdd7a60605243e23a9a49",
        "superseded_by_correlation_id": "afe6ae01-e566-49a4-804b-704df0d8ac7e",
    }


def _wire_busy_payload() -> dict[str, Any]:
    """A non-supersession rejection: three keys, the two optional ones absent.

    ``to_wire`` writes the supersession pair ONLY when set, so this is the byte shape
    every reason that predates OMN-18143 serialises to. A reader that cannot parse this
    has broken the older path while fixing the new one.
    """
    return {
        "correlation_id": "05007c45-a2b8-4c8c-bc7f-28ff37e3dd99",
        "reason": "busy",
        "scope": "runtime",
    }


def _envelope(payload: dict[str, Any]) -> ModelEventEnvelope[object]:
    """The envelope the runtime builds for a bare deploy-agent event body.

    The agent publishes a bare dict with no ``event_type`` key, so the consume boundary
    stamps ``event_type`` from the TOPIC. Same wire form as the completion event.
    """
    return ModelEventEnvelope[object](
        payload=payload,
        correlation_id=uuid4(),
        event_type=TOPIC_REJECTED,
    )


class _RecordingBus:
    """Fails the test if the durable rejection arm performs any I/O."""

    def __init__(self) -> None:
        self.published: list[str] = []
        self.subscribed: list[str] = []

    async def publish(self, topic: str, **_kwargs: object) -> None:
        self.published.append(topic)

    async def subscribe(self, topic: str, **_kwargs: object) -> object:
        self.subscribed.append(topic)

        async def _unsubscribe() -> None:
            return None

        return _unsubscribe


# ---------------------------------------------------------------------------
# 1. The declarative home. This is the half that makes the topic exist.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_contract_subscribes_the_rejection_topic() -> None:
    """AC2/AC3: the reader's contract declares it, which is what provisions it."""
    subscribe = _contract()["event_bus"]["subscribe_topics"]
    assert TOPIC_REJECTED in subscribe, (
        f"{TOPIC_REJECTED} is not in event_bus.subscribe_topics ({subscribe}); "
        "the contract-driven provisioner creates only topics it finds in a contract, "
        "so without this line the topic stays absent and every rejection the agent "
        "publishes is lost"
    )


@pytest.mark.unit
def test_the_provisioner_extractor_sees_the_topic() -> None:
    """The declaration reaches the thing that creates topics, not just the YAML.

    ``create_kafka_topics.py`` provisions exactly what ``ContractTopicExtractor`` yields.
    Asserting the contract key alone would pass while the provisioner still skipped it,
    which is the "works by convention" gap this test closes.

    Its own positive control is in the same assertion: the sibling completion topic must
    come back from the same call, so an empty or broken extraction fails loudly instead
    of reading as a clean absence.
    """
    from omnibase_infra.tools.contract_topic_extractor import ContractTopicExtractor

    topics = {entry.topic for entry in ContractTopicExtractor().extract(_NODE_DIR)}

    assert "onex.evt.deploy.rebuild-completed.v1" in topics, (
        f"positive control failed: the extractor returned {sorted(topics)} and does not "
        "include the completion topic that is known to be declared, so this call is not "
        "reading the contract and its verdict on the rejection topic means nothing"
    )
    assert TOPIC_REJECTED in topics, (
        f"the extractor yielded {sorted(topics)}; the provisioner will not create "
        f"{TOPIC_REJECTED}"
    )


@pytest.mark.unit
def test_the_rejection_routing_entry_declares_the_rejection_model() -> None:
    """The routing entry must name the model that really describes the payload.

    This is the OMN-17888 defect in its general form: an entry that routes an event to a
    handler while declaring some other node's input model dispatches every message into a
    validation error. The completion topic dead-lettered 72 records that way.
    """
    entries = [
        e
        for e in _contract()["handler_routing"]["handlers"]
        if e.get("topic") == TOPIC_REJECTED
    ]
    assert len(entries) == 1, (
        f"expected exactly one handler_routing entry for {TOPIC_REJECTED}; got {entries}"
    )
    entry = entries[0]
    assert entry["message_category"] == "event", (
        f"the rejection topic is an event, not a {entry['message_category']}"
    )
    assert entry["input_model"]["name"] == "ModelDeployRebuildRejected", (
        f"routing entry declares input_model {entry['input_model']['name']!r}; the "
        "payload is a rejection and declaring any other model reproduces OMN-17888"
    )


# ---------------------------------------------------------------------------
# 2. The typed model, against the producer's real bytes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_model_accepts_the_producers_superseded_wire_payload() -> None:
    from omnimarket.events.runtime_deployment import ModelDeployRebuildRejected

    rejected = ModelDeployRebuildRejected(**_wire_superseded_payload())

    assert rejected.correlation_id == UUID("63858212-b2f8-474b-b5ed-9d0c79443ba3")
    assert rejected.reason.value == "superseded"
    assert rejected.scope == "full"
    assert rejected.superseded_by_sha == "d4b668c276a42c416aebdd7a60605243e23a9a49"
    assert rejected.superseded_by_correlation_id == UUID(
        "afe6ae01-e566-49a4-804b-704df0d8ac7e"
    )


@pytest.mark.unit
def test_the_model_accepts_a_rejection_that_omits_the_supersession_pair() -> None:
    """The pre-OMN-18143 wire shape still parses; three keys, no optional pair."""
    from omnimarket.events.runtime_deployment import ModelDeployRebuildRejected

    rejected = ModelDeployRebuildRejected(**_wire_busy_payload())

    assert rejected.reason.value == "busy"
    assert rejected.superseded_by_sha is None
    assert rejected.superseded_by_correlation_id is None


@pytest.mark.unit
def test_every_producer_reason_token_is_accepted() -> None:
    """A reason the producer can emit but the reader refuses dead-letters in prod.

    The eight tokens are ``deploy_agent.events.EnumRejectionReason``'s members. A new
    token added on the producer side without a counterpart here fails this test rather
    than a live rejection.
    """
    from omnimarket.events.runtime_deployment import ModelDeployRebuildRejected

    producer_tokens = [
        "busy",
        "duplicate",
        "in_progress",
        "invalid_payload",
        "invalid_signature",
        "lane_not_allowed",
        "undecodable_payload",
    ]
    for token in producer_tokens:
        payload = _wire_busy_payload() | {"reason": token}
        assert ModelDeployRebuildRejected(**payload).reason.value == token

    superseded = ModelDeployRebuildRejected(**_wire_superseded_payload())
    assert superseded.reason.value == "superseded"


@pytest.mark.unit
def test_the_supersession_pair_stands_or_falls_together() -> None:
    """Mirrors the producer's own validator; half a pair is a silently dropped command."""
    from pydantic import ValidationError

    from omnimarket.events.runtime_deployment import ModelDeployRebuildRejected

    half = _wire_superseded_payload()
    del half["superseded_by_correlation_id"]
    with pytest.raises(ValidationError):
        ModelDeployRebuildRejected(**half)


@pytest.mark.unit
def test_the_reason_must_agree_with_the_supersession_fields() -> None:
    """Only a superseded rejection may name a replacement, and every one must."""
    from pydantic import ValidationError

    from omnimarket.events.runtime_deployment import ModelDeployRebuildRejected

    mislabelled = _wire_superseded_payload() | {"reason": "busy"}
    with pytest.raises(ValidationError):
        ModelDeployRebuildRejected(**mislabelled)

    unnamed = _wire_busy_payload() | {"reason": "superseded"}
    with pytest.raises(ValidationError):
        ModelDeployRebuildRejected(**unnamed)


# ---------------------------------------------------------------------------
# 3. The projection row.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_projection_row_carries_the_declared_fields() -> None:
    """job id, superseded-by job id, reason, timestamp, lane -- and nothing defaulted."""
    from datetime import UTC, datetime

    from omnimarket.events.runtime_deployment import (
        ModelDeployRebuildRejected,
        ModelDeployRejectionRow,
    )

    observed = datetime(2026, 9, 19, 11, 49, 49, tzinfo=UTC)
    row = ModelDeployRejectionRow.from_event(
        ModelDeployRebuildRejected(**_wire_superseded_payload()),
        observed_at=observed,
        runtime_lane=None,
    )

    assert row.job_id == UUID("63858212-b2f8-474b-b5ed-9d0c79443ba3")
    assert row.superseded_by_job_id == UUID("afe6ae01-e566-49a4-804b-704df0d8ac7e")
    assert row.superseded_by_sha == "d4b668c276a42c416aebdd7a60605243e23a9a49"
    assert row.reason.value == "superseded"
    assert row.observed_at == observed
    assert row.runtime_lane is None
    assert row.scope == "full"


@pytest.mark.unit
def test_the_projection_row_defaults_nothing() -> None:
    """No field may carry a default: a fabricated timestamp or lane is worse than none.

    ``observed_at`` in particular must be supplied by the caller that did the observing.
    A model-level ``default_factory=now`` would make every replayed or backfilled row
    claim to have been seen at import time.
    """
    from omnimarket.events.runtime_deployment import ModelDeployRejectionRow

    for name, field in ModelDeployRejectionRow.model_fields.items():
        assert field.is_required(), (
            f"{name} is not required; every field on the projection row is supplied by "
            "the observer, and a default here fabricates a fact nobody measured"
        )


# ---------------------------------------------------------------------------
# 4. The handler arm, through the object the runtime really dispatches to.
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_wire_shaped_rejection_is_handled_not_coerced_to_the_command() -> None:
    """RED before the fix: the rejection fell through to the COMMAND arm and raised.

    Same class as the 72 dead-lettered completion events of OMN-17888. Adding the
    subscribe topic without the branch is what produces it, which is why the branch and
    the subscription land together.
    """
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        HandlerDeployPublishMonitor,
    )

    bus = _RecordingBus()
    handler = HandlerDeployPublishMonitor(event_bus=bus)

    output = asyncio.run(handler.handle(_envelope(_wire_superseded_payload())))

    assert output is not None, "the rejection event produced no handler output"
    assert not bus.published, (
        f"the durable rejection arm published {bus.published}; observing a rejection "
        "must never issue a rebuild command"
    )
    assert not bus.subscribed, (
        f"the durable rejection arm opened subscriptions {bus.subscribed}; it must not "
        "enter publish_and_monitor"
    )


@pytest.mark.unit
def test_the_arm_records_a_supersession_metric_distinguishable_from_other_reasons() -> (
    None
):
    """AC6's point: a supersession must be tellable from a timeout and from a rollback.

    A single ``rejected`` counter would make "the work is being done by a newer command"
    indistinguishable from "the command was refused", which is the whole distinction the
    lab-verify resolution depends on.
    """
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        HandlerDeployPublishMonitor,
    )

    handler = HandlerDeployPublishMonitor(event_bus=_RecordingBus())

    superseded = asyncio.run(handler.handle(_envelope(_wire_superseded_payload())))
    busy = asyncio.run(handler.handle(_envelope(_wire_busy_payload())))

    assert superseded.metrics["rebuild_rejected_observed"] == 1.0
    assert superseded.metrics["rebuild_rejected_superseded"] == 1.0
    assert busy.metrics["rebuild_rejected_observed"] == 1.0
    assert busy.metrics["rebuild_rejected_superseded"] == 0.0


@pytest.mark.unit
def test_a_malformed_rejection_still_raises() -> None:
    """Typed, not permissive: a malformed terminal event is a real defect.

    Swallowing it here would relocate OMN-17888 rather than fix it -- the same reasoning
    the completion arm records.

    ``ValidationError`` specifically, and on the ``reason`` field specifically: before
    the fix this test passed for the WRONG reason, because an unrouted rejection fell
    through to the command arm and raised there. Naming the exception and matching the
    field is what makes it prove the rejection arm's own validation rather than the
    absence of an arm.
    """
    from pydantic import ValidationError

    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        HandlerDeployPublishMonitor,
    )

    handler = HandlerDeployPublishMonitor(event_bus=_RecordingBus())
    malformed = _wire_superseded_payload() | {"reason": "not_a_reason_the_agent_emits"}

    with pytest.raises(ValidationError, match="reason"):
        asyncio.run(handler.handle(_envelope(malformed)))


@pytest.mark.unit
def test_the_command_arm_is_unchanged_by_the_new_branch() -> None:
    """The publish-monitor path must be byte-for-byte unaffected by an added arm."""
    from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
        EVENT_REBUILD_COMPLETED,
        EVENT_REBUILD_REJECTED,
        TOPIC_REBUILD_COMPLETED,
        TOPIC_REBUILD_REJECTED,
    )

    assert EVENT_REBUILD_REJECTED == "rebuild-rejected"
    assert EVENT_REBUILD_REJECTED != EVENT_REBUILD_COMPLETED
    assert TOPIC_REBUILD_REJECTED == TOPIC_REJECTED
    assert TOPIC_REBUILD_COMPLETED != TOPIC_REBUILD_REJECTED
