# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deploy publish-monitor + rollback EFFECT handler (OMN-13211 / B3).

Absorbs the ``node_redeploy`` ``HandlerRedeployKafka`` (publish-monitor) and
``DeploymentAdapterKafka`` (rollback) into one canonical EFFECT node. The only
real I/O in the redeploy decomposition lives here: publish the HMAC-signed
rebuild command to the external ``.201`` deploy agent on
``onex.cmd.deploy.rebuild-requested.v1``, poll ``onex.evt.deploy.rebuild-completed.v1``
for the matching correlation_id, and — on a deploy that succeeded then failed
post-deploy health — publish the rolled-back event on
``onex.evt.omnimarket.redeploy-rolled-back.v1``.

This handler never SSHes, never calls rpk directly, has no subprocess calls. The
actual Docker rebuild and image restore are the external deploy agent's job; this
effect publishes the command, monitors the bus, and records the outcome. The
event bus is DI-injected; topics are resolved from the contract.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
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
    ModelDeployRebuildCommand,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
    ModelDeployRefusedEvent,
    ModelDeployRejectionRow,
    ModelRedeployResult,
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
_DEFAULT_TIMEOUT_S = 600.0
_POLL_INTERVAL_S = 2.0
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
    result: ModelRedeployResult | None, smoke_test: bool
) -> str | None:
    """Return a rollback reason for a successful deploy that fails post-checks.

    A deploy the agent reports as ``failed`` is NOT a rollback — the artifact
    never went live, so the FSM circuit breaker handles it. Rollback is only for a
    deploy that succeeded then failed post-deploy verification: a publish-monitor
    timeout, a failing ``/health`` check, or a requested smoke probe with no live
    runtime proof (fails closed, OMN-9579).
    """
    if result is None:
        return None
    if result.timed_out:
        return "deploy agent timed out before completion; rolling back"
    if not result.success:
        return None
    failing_health = [hc for hc in result.health_checks if hc.status == "fail"]
    if failing_health:
        endpoints = ", ".join(hc.endpoint for hc in failing_health)
        return f"post-deploy health check failed ({endpoints}); rolling back"
    if smoke_test:
        return "post-deploy smoke test failed (no live runtime proof); rolling back"
    return None


class HandlerDeployPublishMonitor:
    """Publish-monitor + rollback effect for the external deploy agent.

    The event bus is DI-injected (any ``ProtocolEventBus`` — ``EventBusInmemory``
    for tests, ``EventBusKafka`` in the runtime). ``handle`` publishes the rebuild
    command, polls for completion, evaluates rollback, and returns the result and
    any rolled-back event as EFFECT events.
    """

    def __init__(
        self,
        event_bus: Any,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        poll_interval_s: float = _POLL_INTERVAL_S,
        publish_record: DeployPublishRecord | None = None,
    ) -> None:
        if event_bus is None:
            raise RuntimeError(  # error-ok: mis-wired constructor
                "HandlerDeployPublishMonitor requires an event_bus. Wire "
                "EventBusKafka in the runtime, or pass EventBusInmemory for tests."
            )
        self._bus: Any = event_bus
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        # OMN-19377 AC5. On disk under ONEX_STATE_DIR, not in this instance: the
        # rebuild this effect waits on recreates its container, and a dispatch past
        # the runtime deadline is replayed from the DLQ to whichever instance is next.
        self._published = publish_record or DeployPublishRecord.from_env()

    @property
    def bus(self) -> Any:
        """The underlying event bus instance."""
        return self._bus

    async def handle(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Route by event name: observe a completion, else publish/monitor the command.

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

        result = await self.publish_and_monitor(command)

        emitted: list[ModelEventEnvelope[Any]] = []
        reason = _rollback_reason(result, command.smoke_test)
        if reason is not None:
            rolled_back = self.rollback(
                correlation_id=command.correlation_id,
                runtime_lane=command.runtime_lane,
                restored_image=command.rollback_target,
                failure_reason=reason,
                failed_phase=EnumRedeployPhase.VERIFY_HEALTH,
            )
            emitted.append(
                ModelEventEnvelope(
                    payload=rolled_back,
                    correlation_id=envelope.correlation_id or command.correlation_id,
                    event_type=TOPIC_ROLLED_BACK,
                )
            )

        return ModelHandlerOutput.for_effect(
            input_envelope_id=envelope.envelope_id,
            correlation_id=envelope.correlation_id or command.correlation_id or uuid4(),
            handler_id=HANDLER_ID,
            events=tuple(emitted),
            metrics={
                "rebuild_success": 1.0 if result.success else 0.0,
                "timed_out": 1.0 if result.timed_out else 0.0,
                "rolled_back": 1.0 if reason is not None else 0.0,
                "rebuild_rejected": 1.0 if result.rejection_reason is not None else 0.0,
                "duplicate_skipped": 0.0,
            },
        )

    def _observe_rebuild_completed(
        self, envelope: ModelEventEnvelope[Any]
    ) -> ModelHandlerOutput[None]:
        """Record one deploy-agent completion under the model that really describes it.

        WHY THIS ARM DOES NOT RESOLVE THE IN-FLIGHT DEPLOY, stated rather than implied.
        ``ServiceHandlerResolver.resolve`` constructs a FRESH handler instance for every
        ``handler_routing`` entry and caches none, so the instance the runtime dispatches
        this event to is not the instance awaiting a completion future inside
        :meth:`publish_and_monitor`. A shared pending-correlation registry would have to
        be class- or module-level mutable state, which would also be wrong across the two
        runtime processes that load this contract. The correlation-scoped subscription
        :meth:`publish_and_monitor` opens is therefore the monitoring path and stays; this
        durable arm is the platform's record that a completion arrived at all.

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
            events=(),
            metrics={
                "rebuild_completed_observed": 1.0,
                "rebuild_completed_success": (
                    1.0 if completed.status == EnumRedeployStatus.SUCCESS else 0.0
                ),
            },
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

        LIKE THE COMPLETION ARM, this does not resolve an in-flight deploy:
        ``ServiceHandlerResolver.resolve`` builds a fresh handler instance per routing
        entry, so this instance cannot see the future another instance is awaiting. It
        is the platform's durable record that the rejection arrived. Ending the wait is
        the job of the correlation-scoped rejection subscription that
        :meth:`publish_and_monitor` opens beside its completion subscription (OMN-19242).

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
            # OMN-19377. The waiting handler releases on its own answer, but it may be
            # gone: timed out, abandoned past the dispatch deadline, or in a container
            # the agent has since recreated. This arm sees every rejection once, on its
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

    async def publish_and_monitor(
        self, command: ModelDeployPublishCommand
    ) -> ModelRedeployResult:
        """Publish the rebuild command and wait for the matching completion event."""
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

        # Resolved by whichever terminal fact arrives first for THIS correlation: the
        # agent's completion, or its rejection (OMN-19242). Until the rejection arm
        # existed the monitor waited the full timeout after the agent had already
        # refused the command, which held the record past the consumer's poll budget.
        completion_future: asyncio.Future[
            ModelDeployRebuildCompleted | ModelDeployRebuildRejected
        ] = asyncio.get_event_loop().create_future()

        async def _on_completion(message: Any) -> None:
            if completion_future.done():
                return
            try:
                payload = _decode_message(message.value)
                if payload.get("correlation_id", "") != corr_id:
                    return  # different rebuild, ignore
                payload = _normalize_completion_payload(payload)
                completion_future.set_result(ModelDeployRebuildCompleted(**payload))
            except Exception as exc:  # boundary-ok: bus message parse
                logger.warning(
                    "Failed to parse rebuild-completed event: %s", exc, exc_info=True
                )

        async def _on_rejection(message: Any) -> None:
            if completion_future.done():
                return
            try:
                payload = _decode_message(message.value)
                if str(payload.get("correlation_id", "")) != corr_id:
                    return  # a different command's rejection, ignore
                completion_future.set_result(ModelDeployRebuildRejected(**payload))
            except Exception as exc:  # boundary-ok: bus message parse
                logger.warning(
                    "Failed to parse rebuild-rejected event: %s", exc, exc_info=True
                )

        unsubscribe = await self._bus.subscribe(
            TOPIC_REBUILD_COMPLETED,
            on_message=_on_completion,
            group_id=f"redeploy-deploy-effect-{corr_id[:8]}",
        )
        unsubscribe_rejected = await self._bus.subscribe(
            TOPIC_REBUILD_REJECTED,
            on_message=_on_rejection,
            group_id=f"redeploy-deploy-effect-rejected-{corr_id[:8]}",
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

        start_time = time.monotonic()
        timed_out = False
        outcome: ModelDeployRebuildCompleted | ModelDeployRebuildRejected | None = None
        try:
            outcome = await asyncio.wait_for(completion_future, timeout=self._timeout_s)
        except TimeoutError:
            timed_out = True
            logger.error(
                "Redeploy timed out after %ss waiting for correlation_id=%s",
                self._timeout_s,
                corr_id,
            )
        finally:
            await unsubscribe()
            await unsubscribe_rejected()

        elapsed = time.monotonic() - start_time

        if isinstance(outcome, ModelDeployRebuildRejected):
            if outcome.reason is EnumDeployRejectionReason.BUSY:
                # The agent committed past the command without a job, so a later copy
                # is the only way this deploy runs.
                self._published.release_busy(command.correlation_id)
            logger.warning(
                "Deploy agent rejected the command; ending the wait",
                extra={
                    "correlation_id": corr_id,
                    "reason": outcome.reason.value,
                    "elapsed_seconds": elapsed,
                },
            )
            return ModelRedeployResult(
                correlation_id=corr_id,
                success=False,
                status=EnumRedeployStatus.FAILED,
                duration_seconds=elapsed,
                timed_out=False,
                rejection_reason=outcome.reason,
                errors=[
                    f"Deploy agent rejected the command: {outcome.reason.value} "
                    f"(correlation_id={corr_id})"
                ],
            )
        completed = outcome

        if timed_out or completed is None:
            return ModelRedeployResult(
                correlation_id=corr_id,
                success=False,
                status=EnumRedeployStatus.FAILED,
                duration_seconds=elapsed,
                timed_out=True,
                errors=[
                    f"Timed out after {self._timeout_s}s waiting for deploy agent "
                    f"completion (correlation_id={corr_id})"
                ],
            )

        phase_results: dict[str, str] = {}
        if completed.phase_results:
            phase_results = {
                "git": completed.phase_results.git.value,
                "core": completed.phase_results.core.value,
                "runtime": completed.phase_results.runtime.value,
                "verification": completed.phase_results.verification.value,
                "publish": completed.phase_results.publish.value,
            }

        duration = (
            completed.duration_seconds if completed.duration_seconds > 0 else elapsed
        )
        success = completed.status == EnumRedeployStatus.SUCCESS

        logger.info(
            "Redeploy completed",
            extra={
                "correlation_id": corr_id,
                "status": completed.status,
                "duration_seconds": duration,
                "git_sha": completed.git_sha,
                "services_restarted": completed.services_restarted,
            },
        )

        return ModelRedeployResult(
            correlation_id=corr_id,
            success=success,
            status=completed.status,
            duration_seconds=duration,
            git_sha=completed.git_sha,
            runtime_lane=completed.runtime_lane,
            image_ref=completed.image_ref,
            image_digest=completed.image_digest,
            services_restarted=completed.services_restarted,
            phase_results=phase_results,
            errors=completed.errors,
            timed_out=False,
            health_checks=list(completed.health_checks),
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
