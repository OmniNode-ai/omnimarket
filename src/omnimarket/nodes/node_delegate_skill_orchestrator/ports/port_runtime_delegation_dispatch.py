# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event-bus backed delegation dispatch port."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import yaml
from omnibase_core.models.delegation.wire import (
    EnumDelegationOperationalOutcome,
    ModelDelegationFailed,
    ModelDelegationProvenance,
)
from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
    ModelDelegationTerminalFailedRoutedV2,
    ModelDelegationTerminalFailedUnroutedV2,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import TypeAdapter, ValidationError

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusTerminalResult,
)
from omnimarket.events.delegation import ModelDelegationRequest
from omnimarket.models.delegation.wire.model_named_test_run_request import (
    ModelNamedTestSpec,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.dispatch_progress import (
    dispatch_stage,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.run_step_recorder import (
    RunStepRecorder,
)

logger = logging.getLogger(__name__)

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_CONFIG_KEY = "delegation_runtime_dispatch"
_V2_TERMINAL_ADAPTER: TypeAdapter[
    ModelDelegationTerminalCompletedV2
    | ModelDelegationTerminalFailedRoutedV2
    | ModelDelegationTerminalFailedUnroutedV2
] = TypeAdapter(
    ModelDelegationTerminalCompletedV2
    | ModelDelegationTerminalFailedRoutedV2
    | ModelDelegationTerminalFailedUnroutedV2
)


class ProtocolDelegationEventBus(Protocol):
    """Event bus surface required by the runtime delegation dispatch port."""

    async def publish(
        self,
        topic: str,
        key: bytes | None,
        value: bytes,
        headers: object = None,
    ) -> None: ...

    async def subscribe(
        self,
        topic: str,
        node_identity: object | None = None,
        on_message: Callable[[object], Awaitable[None]] | None = None,
        **kwargs: object,
    ) -> Callable[[], Awaitable[None]]: ...


class RuntimeDelegationDispatchPort:
    """Dispatch consumer-facing delegation requests into the runtime bus."""

    def __init__(
        self,
        *,
        event_bus: ProtocolDelegationEventBus,
        config: ModelRuntimeDelegationDispatchConfig | None = None,
        command_topic: str | None = None,
        completed_topic: str | None = None,
        failed_topic: str | None = None,
        response_topic: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        runtime_config = config or load_runtime_delegation_dispatch_config()
        completed_override = completed_topic or response_topic
        if command_topic or completed_override or failed_topic:
            runtime_config = runtime_config.model_copy(
                update={
                    "topics": runtime_config.topics.model_copy(
                        update={
                            "command": command_topic or runtime_config.topics.command,
                            "completed": completed_override
                            or runtime_config.topics.completed,
                            "failed": failed_topic or runtime_config.topics.failed,
                        }
                    )
                }
            )
        self._config = runtime_config

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        provenance: ModelDelegationProvenance | None = None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
        no_escalation: bool = False,
        attribution: Mapping[str, str] | None = None,
        model: str | None = None,
        named_tests: ModelNamedTestSpec | None = None,
        trace_id: UUID | None = None,
        queue_wait_ms: int | None = None,
    ) -> dict[str, object]:
        # OMN-21077: tests run where the workspace is, and a deployed lane has no
        # access to the caller's workspace. Refused rather than dropped, so a
        # caller who named tests is told where they run instead of reading an
        # INSUFFICIENT refusal for tests that never ran.
        if named_tests is not None:
            raise ValueError(
                "named tests are not run by this dispatch port; a code answer is "
                "tested against its named tests only on the in-process path, "
                "by the named-test runner that port is given"
            )
        # OMN-20844: the canonical delegation request this port publishes has
        # no model field at the Core floor this package locks, so a model the
        # caller named cannot reach the consumer. Refused rather than dropped,
        # so the call cannot run on a model the caller did not choose.
        if model is not None:
            raise ValueError(
                "model is not carried by this dispatch port's delegation request; "
                "a per-call model runs on your own key through the in-process "
                "path (onex delegate --bus inmemory --model <id>)"
            )
        # OMN-20606: accepted and unused. This port publishes the request to a
        # deployed lane, whose handler stamps the caller onto its own terminal;
        # only the in-process port writes an evidence terminal of its own.
        del attribution
        # OMN-19448: accepted and unused, for the same reason. The deployed
        # lane's handler copies the command's trace_id and measures the queue
        # wait itself; only the in-process port's evidence terminal needs them.
        del trace_id, queue_wait_ms
        # OMN-18931: the canonical delegation request this port publishes does
        # not carry the no-escalation policy at the Core floor this package
        # locks, so a true value cannot reach the consumer. Refused rather than
        # dropped, so the request cannot escalate unseen.
        if no_escalation:
            raise ValueError(
                "no_escalation is not carried by this dispatch port's delegation "
                "request; route the dogfood fault control through the trusted "
                "runtime consumer's port"
            )
        if execution_timeout_seconds < 1:
            raise ValueError("execution_timeout_seconds must be positive")
        if terminal_delivery_margin_seconds < 1:
            raise ValueError("terminal_delivery_margin_seconds must be positive")
        # OMN-13161: the bus runtime path carries its own routing-tier budgets in
        # the downstream delegation chain. When the request omits max_tokens, fall
        # back to the runtime model's contract default rather than forcing a value;
        # the per-backend ceiling is applied on the in-process local path.
        max_tokens_fields: dict[str, int] = (
            {} if max_tokens is None else {"max_tokens": max_tokens}
        )
        # OMN-14349: ModelDelegationRequest.tenant_id already exists (OMN-14058) --
        # this is the missing plumbing that actually populates it on the bus path.
        # A None here is not a silent default; the field stays None and the
        # downstream projection writer's existing OMN-14058 NULL/omitted-key
        # handling applies (never a masked 'omninode' default).
        request = ModelDelegationRequest(
            prompt=prompt,
            task_type=cast("Any", task_type),
            source_session_id=source_session_id,
            source_file_path=source_file_path,
            correlation_id=correlation_id,
            **max_tokens_fields,
            requested_timeout_seconds=execution_timeout_seconds,
            emitted_at=datetime.now(UTC),
            quality_contract_mode=cast("Any", quality_contract_mode),
            acceptance_criteria=acceptance_criteria,
            tenant_id=tenant_id,
            provenance=provenance,
            backend_id=backend_id,
            response_contract=response_contract,
            system_prompt=system_prompt,
            temperature=temperature,
            response_format=response_format,
        )

        # OMN-19972: the run began here. Its steps are numbered once the chain
        # answers, from this moment, and published over this port's own bus.
        run_started_at = datetime.now(UTC)
        run_started = time.monotonic()

        if not wait:
            with dispatch_stage("publish"):
                await self._publish_request(request)
            return {
                "status": "completed",
                "content": "",
                "delegated_to": "runtime",
                "model_name": "",
                "quality_gate_passed": False,
            }

        try:
            with dispatch_stage("subscribe"):
                unsubscribe, queue = await self._subscribe_for_result(correlation_id)
        except TimeoutError as exc:
            return {
                "status": "timeout",
                "error_message": f"delegation timed out at stage=subscribe: {exc}",
            }
        timeout_stage = "publish"
        try:
            with dispatch_stage("publish"):
                await self._publish_request(request)
            timeout_seconds = float(self._config.wait_timeout_seconds)
            timeout_stage = "terminal_wait"
            with dispatch_stage("terminal_wait"):
                terminal = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
        except TimeoutError as exc:
            await self._publish_run_steps(
                correlation_id=correlation_id,
                task_type=task_type,
                tenant_id=tenant_id,
                started_at=run_started_at,
                started=run_started,
                status="timeout",
                payload=None,
            )
            return {
                "status": "timeout",
                "error_message": (
                    f"timed out after {self._config.wait_timeout_seconds}s "
                    "at stage=terminal_wait waiting for delegation result"
                    if timeout_stage == "terminal_wait"
                    else f"delegation timed out at stage=publish: {exc}"
                ),
            }
        finally:
            with dispatch_stage("terminal_cleanup"):
                await _unsubscribe(unsubscribe)

        await self._publish_run_steps(
            correlation_id=correlation_id,
            task_type=task_type,
            tenant_id=tenant_id,
            started_at=run_started_at,
            started=run_started,
            status=terminal.status,
            payload=terminal.payload,
        )
        result: dict[str, object] = {
            "status": terminal.status,
            "correlation_id": str(correlation_id),
        }
        if terminal.error_message:
            result["error_message"] = terminal.error_message
        if terminal.payload:
            result.update(_flatten_terminal_payload(terminal.payload))
        return result

    async def _publish_run_steps(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        tenant_id: str | None,
        started_at: datetime,
        started: float,
        status: str,
        payload: dict[str, object] | None,
    ) -> None:
        """Number the run's steps from its terminal and publish them in order.

        OMN-19972: the downstream chain takes the steps; this node numbers them,
        as its contract declares for ``run_step_event``, so a deployed run
        reaches the run-trace writer the way a local one does. Best-effort, like
        the recorder: a step that cannot be delivered is a gap on the page,
        never a failed delegation.
        """
        flat = {} if payload is None else _flatten_terminal_payload(payload)
        tenant = _text(flat.get("tenant_id")) or _text(tenant_id)
        if tenant is None:
            logger.warning(
                "run steps of correlation_id=%s not published: no tenant on the "
                "request or the terminal",
                correlation_id,
            )
            return
        buffer = _BufferedRunStepPublisher()
        try:
            recorder = RunStepRecorder(
                tenant_id=tenant,
                correlation_id=correlation_id,
                publisher=buffer,
                store=None,
                started_at=started_at,
                started_monotonic=started,
            )
            _record_bus_run(
                recorder,
                task_type=task_type,
                status=status,
                flat=flat if payload is not None else None,
                started_at=started_at,
            )
        except Exception:
            logger.warning(
                "run steps of correlation_id=%s could not be derived",
                correlation_id,
                exc_info=True,
            )
        for step in buffer.pending:
            try:
                envelope = ModelEventEnvelope[dict[str, object]](
                    payload=step.payload,
                    correlation_id=correlation_id,
                    envelope_timestamp=datetime.now(UTC),
                    event_type=step.event_type,
                    source_tool=self._config.source_tool,
                )
                await self._event_bus.publish(
                    step.topic,
                    step.partition_key.encode("utf-8") if step.partition_key else None,
                    envelope.model_dump_json(exclude_none=True).encode("utf-8"),
                    None,
                )
            except Exception:
                logger.warning(
                    "run step %s of correlation_id=%s was not published",
                    step.event_id,
                    correlation_id,
                    exc_info=True,
                )

    async def _publish_request(self, request: ModelDelegationRequest) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest](
            payload=request,
            correlation_id=request.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=self._config.request_message_type,
            source_tool=self._config.source_tool,
        )
        await self._event_bus.publish(
            self._config.topics.command,
            None,
            envelope.model_dump_json(exclude_none=True).encode("utf-8"),
            None,
        )

    async def _subscribe_for_result(
        self, dispatch_correlation_id: UUID
    ) -> tuple[
        Callable[[], Awaitable[None]], asyncio.Queue[ModelDispatchBusTerminalResult]
    ]:
        queue: asyncio.Queue[ModelDispatchBusTerminalResult] = asyncio.Queue()

        async def on_message(message: object) -> None:
            value = _message_value(message)
            if value is None:
                return
            terminal = _parse_delegation_terminal(
                value,
                expected_correlation_id=dispatch_correlation_id,
                failed_topic=self._config.topics.failed,
            )
            if terminal is None:
                return
            await queue.put(terminal)

        async def on_pre_dispatch_failure(message: object) -> None:
            value = _message_value(message)
            if value is None or not _is_pre_dispatch_failure(value):
                return
            # Ordinary routed V1 failures are co-published with V2. They must
            # not race the richer V2 terminal into this request's receipt.
            await on_message(message)

        unsubscribers = []
        try:
            for topic, callback in (
                (self._config.topics.completed, on_message),
                (self._config.topics.failed, on_message),
                (self._config.topics.failed_unrouted, on_message),
                (self._config.topics.pre_dispatch_failed, on_pre_dispatch_failure),
            ):
                if topic is not None:
                    unsubscribers.append(
                        await self._event_bus.subscribe(
                            topic,
                            None,
                            callback,
                            group_id=(
                                f"{self._config.consumer_group_prefix}-{dispatch_correlation_id.hex}"
                            ),
                        )
                    )
        except BaseException:
            # Setup can fail or be cancelled after earlier topics subscribed.
            # Release those consumers before propagating the original failure.
            for unsubscribe_topic in unsubscribers:
                try:
                    await unsubscribe_topic()
                except Exception:
                    logger.warning(
                        "partial delegation subscription cleanup failed", exc_info=True
                    )
            raise

        async def unsubscribe() -> None:
            for unsubscribe_topic in unsubscribers:
                await unsubscribe_topic()

        return unsubscribe, queue


def _is_pre_dispatch_failure(value: bytes | str) -> bool:
    """Validate a canonical zero-attempt boundary failure before accepting V1."""
    try:
        raw = json.loads(value)
        if not isinstance(raw, dict):
            return False
        terminal = ModelDelegationFailed.model_validate(raw.get("payload", raw))
    except (TypeError, ValueError, ValidationError):
        return False
    return (
        terminal.attempts_count == 0
        and terminal.operational_outcome
        is EnumDelegationOperationalOutcome.BOUNDARY_FAILURE
        and bool(terminal.failure_reason)
    )


class _BufferedStep:
    __slots__ = ("event_id", "event_type", "partition_key", "payload", "topic")

    def __init__(
        self,
        *,
        topic: str,
        event_type: str,
        payload: dict[str, object],
        event_id: str,
        partition_key: str | None,
    ) -> None:
        self.topic = topic
        self.event_type = event_type
        self.payload = payload
        self.event_id = event_id
        self.partition_key = partition_key


class _BufferedRunStepPublisher:
    """Holds the recorder's steps, in its order, until the port awaits the bus."""

    def __init__(self) -> None:
        self.pending: list[_BufferedStep] = []

    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        self.pending.append(
            _BufferedStep(
                topic=topic,
                event_type=event_type,
                payload=dict(payload),
                event_id=event_id,
                partition_key=partition_key,
            )
        )
        return True


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _bus_attempts(
    flat: Mapping[str, object], *, status: str
) -> list[Mapping[str, object]]:
    """The chain's attempts, from its escalation ladder or, failing that, the terminal."""
    history = flat.get("escalation_history")
    if isinstance(history, list | tuple):
        ladder = [entry for entry in history if isinstance(entry, Mapping)]
        if ladder:
            return ladder
    cause = flat.get("routed_failure_cause")
    provider_failed = isinstance(cause, Mapping) and cause.get("kind") == "provider"
    failure = _text(flat.get("terminal_failure_reason")) or _text(
        flat.get("failure_reason")
    )
    attempt: dict[str, object] = {
        "backend_ref": flat.get("backend_ref") or flat.get("route"),
        "model_used": flat.get("model_used"),
        "tier": flat.get("cost_tier_name"),
        "latency_ms": flat.get("latency_ms"),
    }
    if status == "completed":
        attempt["acceptance_decision"] = "accept"
    elif provider_failed:
        attempt["acceptance_decision"] = "terminate"
        attempt["acceptance_reason"] = "provider_call_failed"
        attempt["failure_reasons"] = [failure] if failure else []
    else:
        attempt["acceptance_decision"] = "terminate"
        attempt["acceptance_reason"] = (
            _text(cause.get("kind")) if isinstance(cause, Mapping) else None
        ) or "quality_gate_rejection"
        attempt["failure_reasons"] = [failure] if failure else []
    return [attempt]


def _record_bus_run(
    recorder: RunStepRecorder,
    *,
    task_type: str,
    status: str,
    flat: Mapping[str, object] | None,
    started_at: datetime,
) -> None:
    """Take a deployed run's steps in the order the chain took them."""
    recorder.request(task_type=task_type)
    if flat is None:
        recorder.terminal(succeeded=False, failure_cause=status)
        return
    model = _text(flat.get("model_used"))
    backend = _text(flat.get("backend_ref")) or _text(flat.get("route"))
    if backend is None and flat.get("routing_disposition") == "unrouted":
        recorder.terminal(
            succeeded=False,
            model_id=model,
            failure_cause=_text(flat.get("unrouted_reason")) or "unrouted",
        )
        return
    tier = _text(flat.get("cost_tier_name")) or "unknown"
    attempts = _bus_attempts(flat, status=status)
    for index, attempt in enumerate(attempts):
        attempt_backend = _text(attempt.get("backend_ref")) or backend or "unknown"
        attempt_model = _text(attempt.get("model_used")) or model
        recorder.routing(
            backend_id=attempt_backend,
            model_id=attempt_model or "",
            tier=_text(attempt.get("tier")) or tier,
        )
        last = index == len(attempts) - 1
        latency = attempt.get("latency_ms")
        if last and not isinstance(latency, int):
            latency = flat.get("latency_ms")
        reason_code = _text(attempt.get("acceptance_reason"))
        reasons = attempt.get("failure_reasons")
        detail = (
            "; ".join(str(r) for r in reasons)
            if isinstance(reasons, list | tuple) and reasons
            else None
        )
        provider_failed = reason_code == "provider_call_failed"
        recorder.provider_call(
            started_at=started_at,
            duration_ms=latency if isinstance(latency, int) and latency >= 0 else None,
            backend_id=attempt_backend,
            model_id=attempt_model,
            failure_cause=reason_code if provider_failed else None,
            reason=detail if provider_failed else None,
        )
        if provider_failed:
            continue
        accepted = attempt.get("acceptance_decision") == "accept"
        recorder.quality_gate(
            backend_id=attempt_backend,
            failure_cause=None if accepted else (reason_code or "rejected"),
            reason=None if accepted else detail,
        )
    recorder.terminal(
        succeeded=status == "completed", backend_id=backend, model_id=model
    )


def _message_value(message: object) -> bytes | str | None:
    raw = getattr(message, "value", None)
    if isinstance(raw, bytearray):
        return bytes(raw)
    if isinstance(raw, bytes | str):
        return raw
    return None


def _flatten_terminal_payload(payload: dict[str, object]) -> dict[str, object]:
    # OMN-14600: retained for the legacy double-nested wire shape (a bespoke
    # inner envelope carrying its own "topic" + "payload" keys). The runtime
    # now publishes a SINGLE canonical envelope whose payload is the
    # unwrapped ModelDelegationResult directly, which has no "payload" key of
    # its own — the isinstance check below is False and this is a no-op
    # pass-through for that (current) shape.
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        flattened = dict(nested_payload)
        topic = payload.get("topic")
        if isinstance(topic, str) and topic:
            flattened["terminal_topic"] = topic
        payload = flattened
    if "routing_disposition" not in payload:
        return payload
    # V2 carries the routed backend identity and the producer's manifest version.
    # Its quality evaluation is nested; expose the same facts to the receipt.
    flattened = dict(payload)
    flattened["provider"] = payload.get("backend_ref") or ""
    flattened["pricing_manifest_version"] = payload.get("pricing_manifest_version", 0)
    evaluation = payload.get("quality_bar_evaluation")
    if isinstance(evaluation, dict):
        flattened.update(evaluation)
    failure = payload.get("terminal_failure_reason")
    if isinstance(failure, str):
        flattened["failure_reason"] = failure
    return flattened


def _short_topic_alias(topic: str) -> str | None:
    """Derive the '{producer}.{event-name}' alias DispatchResultApplier stamps
    onto ``ModelEventEnvelope.event_type`` (see
    ``service_dispatch_result_applier.py::_derive_event_type_from_topic``,
    OMN-12116). The wire envelope's ``event_type`` carries this derived alias,
    not the full topic string, so a failed/completed comparison against the
    full topic must also check the derived form.
    """
    parts = topic.split(".")
    if len(parts) >= 5 and parts[0] == "onex":
        return f"{parts[2]}.{parts[3]}"
    return None


def _parse_delegation_terminal(
    value: bytes | str,
    *,
    expected_correlation_id: UUID,
    failed_topic: str,
) -> ModelDispatchBusTerminalResult | None:
    try:
        raw = json.loads(value.decode("utf-8") if isinstance(value, bytes) else value)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    envelope_payload = raw.get("payload", raw)
    if not isinstance(envelope_payload, dict):
        return None

    topic = str(envelope_payload.get("topic") or raw.get("event_type") or "")
    wire_payload = envelope_payload.get("payload", envelope_payload)
    if isinstance(wire_payload, dict) and (
        topic.endswith(".v2")
        or any(
            field in wire_payload
            for field in ("routing_disposition", "terminal_outcome", "backend_ref")
        )
    ):
        # Validate before flattening: correlation alone cannot make a URL-shaped
        # backend, absent pricing version or contradictory gate result evidence.
        # The released wire contracts own these invariants (OMN-17013).
        try:
            _V2_TERMINAL_ADAPTER.validate_python(wire_payload)
        except ValidationError:
            return None

    terminal_payload = _flatten_terminal_payload(
        cast(dict[str, object], envelope_payload)
    )
    raw_correlation_id = terminal_payload.get("correlation_id")
    try:
        correlation_id = UUID(str(raw_correlation_id))
    except (TypeError, ValueError):
        return None
    if correlation_id != expected_correlation_id:
        return None

    # OMN-14600: the single canonical envelope has no "topic" key on its
    # (unwrapped) payload, so the primary signal is ``raw["event_type"]`` —
    # which DispatchResultApplier stamps with the DERIVED short alias
    # ("{producer}.{event-name}"), not the full topic string. Compare against
    # both the full topic (legacy / test-simulated shape) and its derived
    # alias so either form classifies correctly; ``failure_reason`` remains
    # the final fallback for shapes that carry neither.
    failed_alias = _short_topic_alias(failed_topic)
    is_failed = (
        terminal_payload.get("terminal_outcome") == "failed"
        or topic == failed_topic
        or (failed_alias is not None and topic == failed_alias)
        or bool(terminal_payload.get("failure_reason"))
    )
    # Preserve the terminal_topic surface for callers even though the flat
    # (single-envelope) shape no longer routes it through
    # _flatten_terminal_payload's nested branch.
    if topic and "terminal_topic" not in terminal_payload:
        terminal_payload["terminal_topic"] = topic
    error_message = str(terminal_payload.get("failure_reason") or "") or None
    return ModelDispatchBusTerminalResult(
        correlation_id=correlation_id,
        status="failed" if is_failed else "completed",
        payload=cast(dict[str, object], envelope_payload),
        error_message=error_message,
    )


async def _unsubscribe(unsubscribe: Callable[[], Awaitable[None]]) -> None:
    await unsubscribe()


def load_runtime_delegation_dispatch_config(
    contract_path: Path = _DEFAULT_CONTRACT_PATH,
) -> ModelRuntimeDelegationDispatchConfig:
    """Load downstream delegation runtime dispatch settings from contract.yaml."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{contract_path} must contain a mapping")

    config = raw.get(_CONFIG_KEY)
    if not isinstance(config, dict):
        raise ValueError(f"{contract_path} missing {_CONFIG_KEY} mapping")

    return ModelRuntimeDelegationDispatchConfig.model_validate(config)


__all__ = [
    "ProtocolDelegationEventBus",
    "RuntimeDelegationDispatchPort",
    "load_runtime_delegation_dispatch_config",
]
