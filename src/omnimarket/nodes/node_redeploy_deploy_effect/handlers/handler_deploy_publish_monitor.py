# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deploy publish-monitor + rollback EFFECT handler (OMN-13211 / B3).

Absorbs the ``node_redeploy`` ``HandlerRedeployKafka`` (publish-monitor) and
``DeploymentAdapterKafka`` (rollback) into one canonical EFFECT node. The only
real I/O in the redeploy decomposition lives here: publish the HMAC-signed
rebuild command to the external ``.201`` deploy agent on
``onex.cmd.deploy.rebuild-requested.v1``, and — when the agent's completion on
``onex.evt.deploy.rebuild-completed.v1`` reports a deploy that succeeded then failed
post-deploy health — emit the rolled-back event on
``onex.evt.omnimarket.redeploy-rolled-back.v1``.

THE COMMAND ARM DOES NOT WAIT FOR THE AGENT (OMN-18143 AC3). A real rebuild takes
about 20 minutes and recreates this effect's own container; the runtime abandons any
dispatch after 600 s (OMN-19355) and quarantines it to the DLQ. Until this change the
command arm waited up to 600 s for the answer, so the first dispatch of every real
deploy was quarantined and replayed, and the serial consumer held every later
deploy-publish command behind it. The command arm now publishes, records the command
under ``ONEX_STATE_DIR`` with what the rollback decision needs, and returns. The
completion is observed where it arrives, by the ``rebuild-completed`` arm on its own
committed consumer group, which reads that record.

This handler never SSHes, never calls rpk directly, has no subprocess calls. The
actual Docker rebuild and image restore are the external deploy agent's job; this
effect publishes the command, observes the agent's terminal events, and records
the outcome. The
event bus is DI-injected; topics are resolved from the contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.events.runtime_deployment import (
    EnumDeployRejectionReason,
    EnumProdGrantReason,
    EnumRedeployPhase,
    EnumRedeployStatus,
    EnumRuntimeLane,
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
    ModelDeployRefusedEvent,
    ModelDeployRejectionRow,
    ModelRedeployRolledBackEvent,
    verify_prod_deploy_grant_binding,
)
from omnimarket.nodes.contract_topics import (
    contract_publish_topics,
    contract_subscribe_topics,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.deploy_publish_record import (
    DeployPublishRecord,
)
from omnimarket.nodes.node_redeploy_deploy_effect.models.model_deploy_publish_command import (
    ModelDeployPublishCommand,
)

logger = logging.getLogger(__name__)

HANDLER_ID = "redeploy-deploy-publish-monitor-effect"

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_DEPLOY_AGENT_HMAC_SECRET_ENV = "DEPLOY_AGENT_HMAC_SECRET"

# Contract-declared topics (no hardcoded strings).
_SUBSCRIBE = contract_subscribe_topics(_CONTRACT)
_PUBLISH = contract_publish_topics(_CONTRACT)


def _topic_with_suffix(topics: tuple[str, ...], suffix: str, section: str) -> str:
    """Resolve exactly one contract topic ending with ``suffix``."""
    matches = [t for t in topics if t.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(
            f"Contract {_CONTRACT} must declare exactly one event_bus.{section} "
            f"topic ending in {suffix!r}; found {matches}"
        )
    return matches[0]


TOPIC_REBUILD_REQUESTED = _topic_with_suffix(
    _PUBLISH, "deploy.rebuild-requested.v1", "publish_topics"
)
TOPIC_REBUILD_COMPLETED = _topic_with_suffix(
    _SUBSCRIBE, "deploy.rebuild-completed.v1", "subscribe_topics"
)
# OMN-18816. Declared on THIS contract because the deploy agent has none of its own,
# and the contract-driven provisioner creates only topics it finds in a contract.
TOPIC_REBUILD_REJECTED = _topic_with_suffix(
    _SUBSCRIBE, "deploy.rebuild-rejected.v1", "subscribe_topics"
)
TOPIC_ROLLED_BACK = _topic_with_suffix(
    _PUBLISH, "redeploy-rolled-back.v1", "publish_topics"
)
TOPIC_DEPLOY_REFUSED = _topic_with_suffix(
    _PUBLISH, "redeploy-deploy-refused.v1", "publish_topics"
)

# OMN-17888. ``envelope.event_type`` reaches a handler in whichever of the two live wire
# forms the producer used: the consume boundary prefers the event body's own
# ``event_type`` -- which the runtime stamps as the alias ``<producer>.<event-name>`` --
# and falls back to the full topic only when the body carries none. The deploy agent
# publishes a bare ``ModelRebuildCompleted.model_dump()`` with no ``event_type`` key, so
# THIS topic arrives in the full form today; reducing both forms to the bare event name
# means a future enveloped publisher does not silently fall through to the command arm.
_EVENT_VERSION_SUFFIX = re.compile(r"\.v\d+$")


def _event_name(event_type: str) -> str:
    """Reduce either live ``event_type`` wire form to the bare event name."""
    return _EVENT_VERSION_SUFFIX.sub("", event_type.strip()).rpartition(".")[2]


# The branch literal ``handle`` compares against, checked at import against the name the
# CONTRACT's own topic reduces to. The literal is what makes the branch statically
# readable -- the repo's branch guards parse ``event_name == "..."`` comparisons out of
# handler source with ``ast`` -- and this equality is what stops it drifting from the
# contract if the topic is ever renamed. A mismatch fails the import rather than routing
# every completion event to the command arm, which is the failure OMN-17888 records.
EVENT_REBUILD_COMPLETED = "rebuild-completed"
if _event_name(TOPIC_REBUILD_COMPLETED) != EVENT_REBUILD_COMPLETED:
    raise ValueError(
        f"branch literal {EVENT_REBUILD_COMPLETED!r} does not match the event name the "
        f"contract's subscribe topic {TOPIC_REBUILD_COMPLETED!r} reduces to "
        f"({_event_name(TOPIC_REBUILD_COMPLETED)!r}); the event arm would be dead"
    )

EVENT_REBUILD_REJECTED = "rebuild-rejected"
if _event_name(TOPIC_REBUILD_REJECTED) != EVENT_REBUILD_REJECTED:
    raise ValueError(
        f"branch literal {EVENT_REBUILD_REJECTED!r} does not match the event name the "
        f"contract's subscribe topic {TOPIC_REBUILD_REJECTED!r} reduces to "
        f"({_event_name(TOPIC_REBUILD_REJECTED)!r}); the event arm would be dead"
    )


def _normalize_completion_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize deploy-agent payloads to the completion model shape."""
    normalized = dict(payload)
    phase_results = dict(normalized.get("phase_results") or {})
    errors = list(normalized.get("errors") or [])

    status = normalized.get("status")
    if status is None:
        non_publish_in_progress = any(
            phase != "publish" and result == "in_progress"
            for phase, result in phase_results.items()
        )
        failed_phase = any(result == "failed" for result in phase_results.values())
        status = (
            EnumRedeployStatus.FAILED.value
            if errors or failed_phase or non_publish_in_progress
            else EnumRedeployStatus.SUCCESS.value
        )
        normalized["status"] = status

    normalized_phase_results = {}
    for phase, result in phase_results.items():
        if result == "in_progress":
            if phase == "publish":
                result = "success"
            elif status == EnumRedeployStatus.FAILED.value:
                result = "failed"
            else:
                result = "pending"
        normalized_phase_results[phase] = result
    normalized["phase_results"] = normalized_phase_results
    return normalized


def _sign_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Attach the deploy-agent HMAC signature when the shared secret is set."""
    secret = os.environ.get(_DEPLOY_AGENT_HMAC_SECRET_ENV, "").strip()
    if not secret:
        return payload
    body_dict = {k: v for k, v in payload.items() if k != "_signature"}
    body = json.dumps(body_dict, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {**body_dict, "_signature": signature}


def _headers_for(topic: str, correlation_id: Any) -> Any:
    """Stamp the FSM run's correlation onto the envelope header (OMN-16939).

    A raw ``bus.publish`` with no ``headers`` lets the bus mint a FRESH
    ``correlation_id`` for the envelope, so the wire record carries a
    correlation that belongs to nothing. Measured on the dev lane 2026-09-06,
    ``onex.cmd.deploy.rebuild-requested.v1`` offset 0: payload
    ``correlation_id`` ``86d5da00-aeb8-47c2-afce-11333f47406c`` against header
    ``correlation_id`` ``ce9eaa2e-6719-44aa-b441-5555e2def0c8``. Anyone
    correlating the deploy agent's work by header -- which is what every
    tracing surface does -- could not find the FSM run that asked for it.
    """
    # Lazily imported, matching the established omnimarket pattern
    # (node_emit_daemon.kafka_publish, node_event_emit_effect): omnibase_infra
    # is a runtime dependency, not a module-scope import of this package.
    from omnibase_infra.event_bus.models import ModelEventHeaders

    return ModelEventHeaders(
        source=HANDLER_ID,
        event_type=topic,
        timestamp=datetime.now(UTC),
        correlation_id=_as_uuid(correlation_id),
    )


def _as_uuid(value: Any) -> UUID:
    """Coerce a correlation id to UUID, failing loud on a non-UUID value."""
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _rollback_reason(
    completed: ModelDeployRebuildCompleted, smoke_test: bool
) -> str | None:
    """Return a rollback reason for a successful deploy that fails post-checks.

    A deploy the agent reports as ``failed`` is NOT a rollback — the artifact
    never went live, so the FSM circuit breaker handles it. Rollback is only for a
    deploy that succeeded then failed post-deploy verification: a failing
    ``/health`` check, or a requested smoke probe with no live runtime proof (fails
    closed, OMN-9579).

    There is no timeout reason any more (OMN-18143). The effect used to roll back a
    deploy whose answer had not arrived after 600 s, but a real rebuild takes about 20
    minutes, so that rollback called a healthy rebuild still in progress a failure,
    from a dispatch the runtime had already abandoned and quarantined. Silence from the agent is the deploy agent's and the lab convergence
    guard's to report (OMN-18144), not a rollback.
    """
    if completed.status != EnumRedeployStatus.SUCCESS:
        return None
    failing_health = [hc for hc in completed.health_checks if hc.status == "fail"]
    if failing_health:
        endpoints = ", ".join(hc.endpoint for hc in failing_health)
        return f"post-deploy health check failed ({endpoints}); rolling back"
    if smoke_test:
        return "post-deploy smoke test failed (no live runtime proof); rolling back"
    return None


class HandlerDeployPublishMonitor:
    """Publish + rollback effect for the external deploy agent.

    The event bus is DI-injected (any ``ProtocolEventBus`` — ``EventBusInmemory``
    for tests, ``EventBusKafka`` in the runtime). ``handle`` publishes the rebuild
    command and returns; on the agent's completion it evaluates rollback from the
    durable record and returns any rolled-back event as an EFFECT event.
    """

    def __init__(
        self,
        event_bus: Any,
        publish_record: DeployPublishRecord | None = None,
    ) -> None:
        if event_bus is None:
            raise RuntimeError(  # error-ok: mis-wired constructor
                "HandlerDeployPublishMonitor requires an event_bus. Wire "
                "EventBusKafka in the runtime, or pass EventBusInmemory for tests."
            )
        self._bus: Any = event_bus
        # OMN-19377 AC5, OMN-18143 AC3. On disk under ONEX_STATE_DIR, not in this
        # instance: the rebuild this effect publishes recreates its container, the
        # runtime builds a fresh instance per routing entry, and the completion that
        # settles a command reaches a different instance, often in a new container.
        self._published = publish_record or DeployPublishRecord.from_env()

    @property
    def bus(self) -> Any:
        """The underlying event bus instance."""
        return self._bus

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Route by event name: settle a completion, observe a rejection, else publish.

        This contract subscribes to TWO topics of two different categories — the
        publish-monitor COMMAND and the deploy agent's completion EVENT — and the
        runtime dispatches both here. ``rebuild-completed`` therefore has its own arm
        (OMN-17888); without it every completion event was coerced to the command model
        and raised, which dead-lettered 72 of them on the .201 dev lane between
        2026-09-11T13:12:41Z and 2026-09-16T11:28:20Z.

        Defense-in-depth (OMN-13440): on the COMMAND arm, before any deploy-agent I/O,
        the EFFECT independently verifies a prod command is target-bound to a verified
        promotion grant. A prod command with no/mismatched/expired grant is REFUSED
        here — the deploy-agent rebuild command is NEVER published — and a typed
        ``ModelDeployRefusedEvent`` is emitted instead. Non-prod lanes are
        unaffected (the binding check returns ``None``), so dev/stability dispatch
        is byte-for-byte unchanged.
        """
        event_name = _event_name(envelope.event_type or "")

        if event_name == EVENT_REBUILD_COMPLETED:
            return self._observe_rebuild_completed(envelope)

        if event_name == EVENT_REBUILD_REJECTED:
            return self._observe_rebuild_rejected(envelope)

        command = _coerce_command(envelope.payload)

        refusal = verify_prod_deploy_grant_binding(
            runtime_lane=command.runtime_lane,
            image_digest=command.image_digest,
            promotion_batch_id=command.promotion_batch_id,
            grant=command.promotion_grant,
            evaluated_at=command.evaluated_at,
        )
        if refusal is not None:
            return await self._refuse(envelope, command, refusal)

        # OMN-19377. A correlation whose rebuild command already reached the broker is
        # not published again: the agent keeps a job for every command it accepts and
        # refuses a repeat as ``duplicate``, and a command still queued behind a running
        # job is consumed when the agent gets to it. On 2026-09-23 this effect sent
        # 52,414 copies of one decision; on 2026-09-24/25, after the in-process memory
        # landed, 50 more copies of 43 commands arrived through DLQ replays and
        # redeliveries to a recreated container, which that memory could not see.
        if self._published.contains(command.correlation_id):
            return self._skip_answered_repeat(envelope, command)

        await self.publish_rebuild_command(command)

        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            events=(),
            metrics={"rebuild_published": 1.0, "duplicate_skipped": 0.0},
        )

    def _observe_rebuild_completed(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Record one deploy-agent completion, and decide rollback for it (OMN-18143).

        THIS ARM IS WHERE A DEPLOY'S OUTCOME IS DECIDED. The command arm returns once
        the command is published, so no instance is waiting for this event. The state
        the decision needs cannot live on an instance either:
        ``ServiceHandlerResolver.resolve`` constructs a FRESH handler instance for every
        ``handler_routing`` entry and caches none, and the rebuild this event answers
        has usually recreated the container. It lives in the durable record the command
        arm wrote, and :meth:`_settle_completion` reads it, once per correlation.

        It is typed, not permissive: a completion that does not validate still raises and
        still dead-letters, because a malformed terminal event from the deploy agent is a
        real defect and swallowing it here would relocate OMN-17888 rather than fix it.
        """
        payload = envelope.payload
        raw = (
            dict(payload)
            if isinstance(payload, Mapping)
            else payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
        if not isinstance(raw, dict):
            raise TypeError(
                f"deploy-agent completion payload must be a mapping or a model; "
                f"got {type(payload).__name__}"
            )
        completed = ModelDeployRebuildCompleted(**_normalize_completion_payload(raw))
        rolled_back = self._settle_completion(completed)

        logger.info(
            "Deploy-agent rebuild completion observed",
            extra={
                "correlation_id": completed.correlation_id,
                "status": completed.status.value,
                "runtime_lane": (
                    completed.runtime_lane.value
                    if completed.runtime_lane is not None
                    else None
                ),
                "git_sha": completed.git_sha,
                "duration_seconds": completed.duration_seconds,
                "services_restarted": completed.services_restarted,
                "topic": TOPIC_REBUILD_COMPLETED,
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            events=(
                (
                    ModelEventEnvelope(
                        payload=rolled_back,
                        correlation_id=rolled_back.correlation_id,
                        event_type=TOPIC_ROLLED_BACK,
                    ),
                )
                if rolled_back is not None
                else ()
            ),
            metrics={
                "rebuild_completed_observed": 1.0,
                "rebuild_completed_success": (
                    1.0 if completed.status == EnumRedeployStatus.SUCCESS else 0.0
                ),
                "rolled_back": 1.0 if rolled_back is not None else 0.0,
            },
        )

    def _settle_completion(
        self, completed: ModelDeployRebuildCompleted
    ) -> ModelRedeployRolledBackEvent | None:
        """Decide rollback for a completion of a command THIS effect published (OMN-18143).

        The command arm returned long before this completion arrived, and the rebuild
        it answers has usually recreated the container since, so everything the
        decision needs comes from the durable record: the command's rollback target
        and smoke flag. :meth:`DeployPublishRecord.settle` hands the entry back once,
        so a redelivered completion decides nothing and emits nothing: the
        orchestrator terminalises every rolled-back fact it receives (OMN-16939).

        A completion with no entry is observed only. It answers a command this effect
        did not publish, or one published before the record carried a rollback target,
        and a rollback it cannot name a target for is not one to emit.
        """
        try:
            correlation_id = _as_uuid(completed.correlation_id)
        except ValueError:
            logger.warning(
                "Deploy-agent completion with a non-UUID correlation; observed only",
                extra={"correlation_id": completed.correlation_id},
            )
            return None
        entry = self._published.settle(correlation_id, outcome=completed.status.value)
        if entry is None:
            return None
        rollback_target = entry.get("rollback_target")
        if not isinstance(rollback_target, str) or not rollback_target:
            logger.warning(
                "Completion of a published rebuild command whose record carries no "
                "rollback target; no rollback is decided for it",
                extra={"correlation_id": str(correlation_id)},
            )
            return None
        reason = _rollback_reason(completed, bool(entry.get("smoke_test", False)))
        if reason is None:
            return None
        return self.rollback(
            correlation_id=correlation_id,
            runtime_lane=EnumRuntimeLane(entry["runtime_lane"]),
            restored_image=rollback_target,
            failure_reason=reason,
            failed_phase=EnumRedeployPhase.VERIFY_HEALTH,
        )

    def _observe_rebuild_rejected(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Record one deploy-agent rejection: the terminal event for work that will not run.

        WHY THIS ARM EXISTS AT ALL (OMN-18816). Until 2026-09-19 nothing anywhere
        subscribed this topic, so it had no declarative home and did not exist on the
        dev lane broker. The agent published into absent cluster metadata and retried
        for 46 minutes. Subscribing it here is what gives the contract-driven
        provisioner a topic to create, so the reader and the topic land together and
        neither can exist without the other.

        WHY THE SUPERSESSION METRIC IS SEPARATE, and not one ``rejected`` counter.
        ``SUPERSEDED`` is the only reason that is not a refusal: the work IS being done,
        by the newer command the event names. Folding it in with ``busy`` and
        ``invalid_signature`` would erase the one distinction that lets a lab-verify
        guard resolve a coalesced sha as PASS instead of timing out on a rebuild that
        will never run under that name. Two of the four merges of 2026-09-19 needed
        exactly that distinction and did not get it.

        No instance waits on this event (OMN-18143): the command arm returns once the
        command is published. This arm is the platform's durable record that the
        rejection arrived, and the one place a ``busy`` releases the publish record.

        Typed, not permissive: a rejection that does not validate still raises and still
        dead-letters. A malformed terminal event from the deploy agent is a real defect,
        and swallowing it here would relocate OMN-17888 rather than fix it.
        """
        payload = envelope.payload
        raw = (
            dict(payload)
            if isinstance(payload, Mapping)
            else payload.model_dump(mode="json")
            if hasattr(payload, "model_dump")
            else payload
        )
        if not isinstance(raw, dict):
            raise TypeError(
                f"deploy-agent rejection payload must be a mapping or a model; "
                f"got {type(payload).__name__}"
            )
        rejected = ModelDeployRebuildRejected(**raw)

        # The two facts the wire body does not carry. ``observed_at`` is taken HERE,
        # at the moment of observation, and is named for that rather than presented as
        # the time of the rejection. ``runtime_lane`` is an honest None: the producer
        # does not send one, and inventing a lane on a rejection would attribute a
        # refused command to a lane nobody measured.
        row = ModelDeployRejectionRow.from_event(
            rejected,
            observed_at=datetime.now(UTC),
            runtime_lane=None,
        )

        is_superseded = rejected.reason is EnumDeployRejectionReason.SUPERSEDED
        if rejected.reason is EnumDeployRejectionReason.BUSY:
            # OMN-19377. The command arm does not wait for an answer (OMN-18143), so this
            # arm is the only place a busy is seen. It sees every rejection once, on its
            # own committed consumer group, so a busy never leaves the correlation
            # marked as published while the agent holds no job for it.
            self._published.release_busy(rejected.correlation_id)
        logger.info(
            "Deploy-agent rebuild rejection observed",
            extra={
                "job_id": str(row.job_id),
                "reason": row.reason.value,
                "scope": row.scope,
                "observed_at": row.observed_at.isoformat(),
                "runtime_lane": None,
                "superseded_by_job_id": (
                    str(row.superseded_by_job_id)
                    if row.superseded_by_job_id is not None
                    else None
                ),
                "superseded_by_sha": row.superseded_by_sha,
                "topic": TOPIC_REBUILD_REJECTED,
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            events=(),
            metrics={
                "rebuild_rejected_observed": 1.0,
                "rebuild_rejected_superseded": 1.0 if is_superseded else 0.0,
            },
        )

    def _skip_answered_repeat(
        self,
        envelope: ModelEventEnvelope[Any],
        command: ModelDeployPublishCommand,
    ) -> ModelHandlerOutput[None]:
        """Answer a repeat of a published correlation without asking the agent again.

        Nothing is published and nothing is subscribed. The skip is its own outcome,
        not a rejection by the agent, a timeout or a rollback, so that a storm of
        repeats is visible as one and never reads as the agent refusing work.
        """
        logger.warning(
            "Deploy-publish repeat for a correlation whose rebuild command was "
            "already published; not publishing it again",
            extra={
                "correlation_id": str(command.correlation_id),
                "envelope_id": str(envelope.envelope_id),
                "record": str(self._published.path),
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id,
            handler_id=HANDLER_ID,
            events=(),
            metrics={
                "rebuild_success": 0.0,
                "timed_out": 0.0,
                "rolled_back": 0.0,
                "rebuild_rejected": 0.0,
                "rebuild_published": 0.0,
                "duplicate_skipped": 1.0,
            },
        )

    async def _refuse(
        self,
        envelope: ModelEventEnvelope[Any],
        command: ModelDeployPublishCommand,
        refusal: tuple[EnumProdGrantReason, str],
    ) -> ModelHandlerOutput[None]:
        """Refuse an off-gate prod deploy: emit the typed refusal, publish NOTHING else.

        The deploy-agent rebuild command is NEVER published on a refusal — the whole
        point of the EFFECT-boundary check is that a prod deploy that is not target-
        bound to a verified grant never reaches the deploy agent. The refusal is
        recorded as a durable bus fact on the contract-declared refused topic.
        """
        reason, detail = refusal
        grant = command.promotion_grant
        refused = ModelDeployRefusedEvent(
            correlation_id=command.correlation_id,
            runtime_lane=command.runtime_lane,
            requested_image_digest=command.image_digest,
            promotion_batch_id=command.promotion_batch_id,
            grant_id=grant.grant_id if grant is not None else None,
            reason=reason,
            detail=detail,
        )
        await self._bus.publish(
            TOPIC_DEPLOY_REFUSED,
            key=str(command.correlation_id).encode(),
            value=json.dumps(refused.model_dump(mode="json")).encode(),
            headers=_headers_for(TOPIC_DEPLOY_REFUSED, command.correlation_id),
        )
        logger.warning(
            "Prod deploy refused at the EFFECT boundary",
            extra={
                "correlation_id": str(command.correlation_id),
                "runtime_lane": command.runtime_lane.value,
                "reason": reason.value,
                "image_digest": command.image_digest,
                "promotion_batch_id": command.promotion_batch_id,
                "topic": TOPIC_DEPLOY_REFUSED,
            },
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id,
            handler_id=HANDLER_ID,
            events=(
                ModelEventEnvelope(
                    payload=refused,
                    correlation_id=envelope.correlation_id or command.correlation_id,
                    event_type=TOPIC_DEPLOY_REFUSED,
                ),
            ),
            metrics={
                "rebuild_success": 0.0,
                "timed_out": 0.0,
                "rolled_back": 0.0,
                "deploy_refused": 1.0,
            },
        )

    async def publish_rebuild_command(self, command: ModelDeployPublishCommand) -> None:
        """Publish the signed rebuild command and record it. Does not wait for an answer.

        The answer arrives on the agent's completion or rejection topic, usually after
        this effect's container has been recreated by the rebuild itself, and is read
        by the durable arms of :meth:`handle` (OMN-18143 AC3).
        """
        corr_id = str(command.correlation_id)

        rebuild_command = ModelDeployRebuildCommand(
            correlation_id=corr_id,
            requested_by=command.requested_by,
            scope=command.scope,
            runtime_lane=command.runtime_lane,
            build_source=command.build_source,
            services=list(command.services),
            git_ref=command.git_ref,
            image_ref=command.image_ref,
            image_digest=command.image_digest,
            requested_at=command.requested_at,
        )

        # OMN-18121: an unstated ref is OMITTED from the wire, never sent as a
        # literal or as null. The deploy agent's own ModelRebuildRequested
        # defaults git_ref from DEPLOY_AGENT_TRACKING_REF, the branch the target
        # lane DECLARES it tracks (OMN-16442) — so omitting the key lets the
        # lane answer for itself, which is the only party that knows. Sending a
        # ref this side invented is what reset the shared deploy clone onto the
        # release branch five times.
        rebuild_payload = rebuild_command.model_dump(mode="json")
        if rebuild_payload.get("git_ref") is None:
            rebuild_payload.pop("git_ref", None)
        # OMN-19270: likewise an unknown request time is omitted, so the agent
        # falls back to the record's own timestamp rather than reading null.
        if rebuild_payload.get("requested_at") is None:
            rebuild_payload.pop("requested_at", None)
        command_payload = _sign_envelope(rebuild_payload)
        # Staged before the publish, so a completion that overtakes the record write
        # below can still be settled with this command's rollback target. A staged
        # command does not count as published (OMN-18143).
        self._published.stage(
            command.correlation_id,
            runtime_lane=command.runtime_lane.value,
            git_ref=command.git_ref,
            rollback_target=command.rollback_target,
            smoke_test=command.smoke_test,
        )
        publish_started_at = datetime.now(UTC)
        await self._bus.publish(
            TOPIC_REBUILD_REQUESTED,
            key=corr_id.encode(),
            value=json.dumps(command_payload).encode(),
            headers=_headers_for(TOPIC_REBUILD_REQUESTED, command.correlation_id),
        )
        # Recorded only once the publish returned, so a publish that raised leaves no
        # record and its redelivery is published (OMN-19377 AC4).
        self._published.record(
            command.correlation_id,
            runtime_lane=command.runtime_lane.value,
            git_ref=command.git_ref,
            rollback_target=command.rollback_target,
            smoke_test=command.smoke_test,
            publish_started_at=publish_started_at,
        )
        logger.info(
            "Redeploy command published",
            extra={
                "correlation_id": corr_id,
                "scope": command.scope.value,
                "runtime_lane": command.runtime_lane.value,
                "build_source": command.build_source.value,
                "git_ref": command.git_ref,
                "image_digest": command.image_digest,
                "topic": TOPIC_REBUILD_REQUESTED,
            },
        )

    def rollback(
        self,
        correlation_id: Any,
        runtime_lane: Any,
        restored_image: str,
        failure_reason: str,
        failed_phase: EnumRedeployPhase,
    ) -> ModelRedeployRolledBackEvent:
        """Build the rolled-back fact. Does NOT publish it (OMN-16939).

        The image restore itself is the deploy agent's job; this records the
        rollback decision, and ``handle`` returns it as a handler-output event
        so the RUNTIME publishes it exactly once on the contract-declared topic.

        This method used to ALSO ``self._bus.publish`` the bare payload while
        ``handle`` returned the same fact for the runtime to publish, so every
        logical rollback produced TWO broker records on
        ``onex.evt.omnimarket.redeploy-rolled-back.v1`` a few milliseconds apart
        -- one bare payload, one full envelope. Measured on the dev lane
        2026-09-06: offsets 0 (bare, 21:06:49.853307Z) and 1 (envelope,
        21:06:49.859946Z) for correlation 86d5da00, and the same pairing for
        every other correlation on the topic.

        That duplicate is not cosmetic. ``node_redeploy_orchestrator``
        subscribes to this topic and terminalizes each arrival, so two
        rolled-back records became TWO ``redeploy-completed`` terminals per
        logical run -- observed at offsets 6378/6379, 6380/6381 and 6382/6383,
        each pair carrying a distinct ``envelope_timestamp`` because the
        orchestrator genuinely ran twice.

        The in-memory-bus tests passed throughout because the in-memory bus has
        no dispatch-result applier: it only ever saw the direct publish, so
        "exactly one record on the topic" was true in the test and false on the
        runtime. The tests now assert the handler publishes NOTHING itself.
        """
        return ModelRedeployRolledBackEvent(
            correlation_id=correlation_id,
            runtime_lane=runtime_lane,
            restored_image=restored_image,
            failure_reason=failure_reason,
            failed_phase=failed_phase,
        )


def _decode_message(raw: Any) -> Any:
    """Decode one bus message value into its JSON payload."""
    if isinstance(raw, bytes | bytearray):
        return json.loads(raw.decode())
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _coerce_command(payload: Any) -> ModelDeployPublishCommand:
    """Coerce the dispatched payload into a ``ModelDeployPublishCommand``."""
    if isinstance(payload, ModelDeployPublishCommand):
        return payload
    if isinstance(payload, Mapping):
        return ModelDeployPublishCommand.model_validate(dict(payload))
    if hasattr(payload, "model_dump"):
        return ModelDeployPublishCommand.model_validate(payload.model_dump())
    raise TypeError(
        f"deploy publish payload must be ModelDeployPublishCommand or a mapping; "
        f"got {type(payload).__name__}"
    )


__all__: list[str] = [
    "EVENT_REBUILD_COMPLETED",
    "HANDLER_ID",
    "TOPIC_DEPLOY_REFUSED",
    "TOPIC_REBUILD_COMPLETED",
    "TOPIC_REBUILD_REQUESTED",
    "TOPIC_ROLLED_BACK",
    "HandlerDeployPublishMonitor",
]
