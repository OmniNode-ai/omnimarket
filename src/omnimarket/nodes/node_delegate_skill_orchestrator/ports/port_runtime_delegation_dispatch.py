# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event-bus backed delegation dispatch port."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

import yaml
from omnibase_core.models.delegation.wire import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusTerminalResult,
)
from omnimarket.events.delegation import ModelDelegationRequest
from omnimarket.models.delegation.wire.model_dispatch_policy import (
    DispatchPolicy,
    canonical_execution_binding_type,
    canonical_first_effect_authorization_binding_type,
    is_backend_pinned_single_attempt,
    parse_canonical_execution_binding,
    validate_backend_pinned_single_attempt_binding,
    validate_backend_pinned_single_attempt_render_digest,
    validate_canonical_first_effect_authorization_binding,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
)

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_CONFIG_KEY = "delegation_runtime_dispatch"


def _runtime_supports_pinned_single_attempt() -> bool:
    """Return whether Core supplies both canonical selected-policy DTOs.

    This deliberately does *not* inspect optional request fields.  A supported
    runtime also needs an actual composition-root activation at construction;
    otherwise ``dispatch`` remains closed before subscribe/publish.
    """
    return (
        canonical_execution_binding_type() is not None
        and canonical_first_effect_authorization_binding_type() is not None
    )


class ProtocolVerifiedPinnedAuthorizationSource(Protocol):
    """Infra composition-root source for an issuer-verified Core authorization.

    Implementing this protocol is reserved for the canonical Infra ingress /
    outbox authority after it has verified issuer provenance and one-use
    authorization.  Receiving a Core DTO, or comparing its fields to request
    pins, is *not* proof of issuer provenance; the checks below only detect
    request association after that external verification has already occurred.
    Public handlers and raw callers have no way to provide this dependency.
    """

    def __call__(
        self,
        correlation_id: UUID,
        tenant_id: str,
        backend_id: str,
        rendered_contract_sha256: str,
        dispatch_policy: DispatchPolicy,
    ) -> object: ...


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
        verified_pinned_authorization_source: ProtocolVerifiedPinnedAuthorizationSource
        | None = None,
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
        # This dependency is installed only by an issuer-verifying Infra
        # composition root.  DTO shape/pin equality does not establish that
        # provenance, so there is deliberately no production default.
        # It is intentionally absent from ``dispatch`` so a CLI/raw wire caller
        # cannot manufacture an activation alongside client-owned tenant input.
        self._verified_pinned_authorization_source = (
            verified_pinned_authorization_source
        )

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
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        backend_id: str | None = None,
        dispatch_policy: DispatchPolicy | None = None,
        rendered_contract_sha256: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if dispatch_policy is not None and not is_backend_pinned_single_attempt(
            dispatch_policy
        ):
            raise ValueError(f"unknown dispatch_policy: {dispatch_policy!r}")
        validate_backend_pinned_single_attempt_render_digest(
            dispatch_policy=dispatch_policy,
            rendered_contract_sha256=rendered_contract_sha256,
        )
        if is_backend_pinned_single_attempt(dispatch_policy):
            validate_backend_pinned_single_attempt_binding(
                dispatch_policy=dispatch_policy,
                backend_id=backend_id,
                tenant_id=tenant_id,
            )
            if not wait:
                raise ValueError(
                    "backend-pinned-single-attempt.v1 requires wait=True so the "
                    "caller receives the required request-bound terminal receipt"
                )
            verified_authorization = self._resolve_verified_pinned_authorization(
                correlation_id=correlation_id,
                tenant_id=tenant_id,
                backend_id=backend_id,
                rendered_contract_sha256=rendered_contract_sha256,
                dispatch_policy=cast("DispatchPolicy", dispatch_policy),
            )
        else:
            verified_authorization = None

        # OMN-13161: the bus runtime path carries its own routing-tier budgets in
        # the downstream delegation chain. When the request omits max_tokens, fall
        # back to the runtime model's contract default rather than forcing a value;
        # the per-backend ceiling is applied on the in-process local path.
        max_tokens_fields: dict[str, int] = (
            {} if max_tokens is None else {"max_tokens": max_tokens}
        )
        # Omit absent optional authority from the command's JSON shape. This
        # preserves the established unpinned command byte shape while a selected
        # policy is always forwarded as an explicit, closed Core DTO field.
        policy_fields: dict[str, object] = {}
        if dispatch_policy is not None:
            policy_fields["dispatch_policy"] = dispatch_policy
        if rendered_contract_sha256 is not None:
            policy_fields["rendered_contract_sha256"] = rendered_contract_sha256
        if verified_authorization is not None:
            policy_fields["first_effect_authorization_binding"] = verified_authorization
        # OMN-14349: ModelDelegationRequest.tenant_id already exists (OMN-14058) --
        # this is the missing plumbing that actually populates it on the bus path.
        # A None here is not a silent default; the field stays None and the
        # downstream projection writer's existing OMN-14058 NULL/omitted-key
        # handling applies (never a masked 'omninode' default).
        try:
            request = ModelDelegationRequest(
                prompt=prompt,
                task_type=cast("Any", task_type),
                source_session_id=source_session_id,
                source_file_path=source_file_path,
                correlation_id=correlation_id,
                **max_tokens_fields,
                emitted_at=datetime.now(UTC),
                quality_contract_mode=cast("Any", quality_contract_mode),
                acceptance_criteria=acceptance_criteria,
                tenant_id=tenant_id,
                backend_id=backend_id,
                **policy_fields,
                response_contract=response_contract,
                system_prompt=system_prompt,
                temperature=temperature,
                response_format=response_format,
            )
        except ValueError as exc:
            if verified_authorization is not None:
                raise RuntimeError(
                    "backend-pinned-single-attempt.v1 requires the audited Core "
                    "request authority seam; refusing before runtime subscribe or publish"
                ) from exc
            raise

        if not wait:
            await self._publish_request(request)
            return {
                "status": "completed",
                "content": "",
                "delegated_to": "runtime",
                "model_name": "",
                "quality_gate_passed": False,
            }

        unsubscribe, queue = await self._subscribe_for_result(correlation_id)
        try:
            await self._publish_request(request)
            timeout_seconds = float(self._config.wait_timeout_seconds)
            terminal = await asyncio.wait_for(queue.get(), timeout=timeout_seconds)
        except TimeoutError:
            return {
                "status": "timeout",
                "error_message": (
                    f"timed out after {self._config.wait_timeout_seconds}s "
                    "waiting for delegation result"
                ),
            }
        finally:
            await _unsubscribe(unsubscribe)

        result: dict[str, object] = {
            "status": terminal.status,
            "correlation_id": str(correlation_id),
        }
        if terminal.error_message:
            result["error_message"] = terminal.error_message
        if terminal.payload:
            result.update(_flatten_terminal_payload(terminal.payload))
        _validate_runtime_execution_binding(
            dispatch_policy=dispatch_policy,
            backend_id=backend_id,
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            rendered_contract_sha256=rendered_contract_sha256,
            status=terminal.status,
            result=result,
        )
        _add_pinned_attempt_receipt(
            dispatch_policy=dispatch_policy,
            result=result,
        )
        return result

    def _resolve_verified_pinned_authorization(
        self,
        *,
        correlation_id: UUID,
        tenant_id: str | None,
        backend_id: str | None,
        rendered_contract_sha256: str | None,
        dispatch_policy: DispatchPolicy,
    ) -> object:
        """Get an issuer-verified Core authorization from the Infra root.

        This port cannot verify issuer provenance itself.  Until Infra wires a
        canonical source, it must remain closed rather than treating a Core DTO
        instance, a mapping, or matching request fields as authority.
        """
        source = self._verified_pinned_authorization_source
        if source is None:
            raise RuntimeError(
                "backend-pinned-single-attempt.v1 requires an issuer-verified Infra "
                "composition-root authorization; refusing before runtime subscribe or publish"
            )
        assert tenant_id is not None
        assert backend_id is not None
        assert rendered_contract_sha256 is not None
        activation = source(
            correlation_id,
            tenant_id,
            backend_id,
            rendered_contract_sha256,
            dispatch_policy,
        )
        return validate_canonical_first_effect_authorization_binding(
            activation,
            correlation_id=correlation_id,
            tenant_id=tenant_id,
            backend_id=backend_id,
            rendered_contract_sha256=rendered_contract_sha256,
            dispatch_policy=dispatch_policy,
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

        unsubscribe_completed = await self._event_bus.subscribe(
            self._config.topics.completed,
            None,
            on_message,
            group_id=(
                f"{self._config.consumer_group_prefix}-{dispatch_correlation_id.hex}"
            ),
        )
        unsubscribe_failed = await self._event_bus.subscribe(
            self._config.topics.failed,
            None,
            on_message,
            group_id=(
                f"{self._config.consumer_group_prefix}-{dispatch_correlation_id.hex}"
            ),
        )

        async def unsubscribe() -> None:
            await unsubscribe_completed()
            await unsubscribe_failed()

        return unsubscribe, queue


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
        return flattened
    return payload


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
    topic = str(envelope_payload.get("topic") or raw.get("event_type") or "")
    failed_alias = _short_topic_alias(failed_topic)
    is_failed = (
        topic == failed_topic
        or (failed_alias is not None and topic == failed_alias)
        or bool(terminal_payload.get("failure_reason"))
    )
    terminal_type = ModelDelegationFailed if is_failed else ModelDelegationCompleted
    canonical_input = dict(terminal_payload)
    canonical_input.pop("terminal_topic", None)
    try:
        canonical_terminal = terminal_type.model_validate(canonical_input)
    except ValueError:
        return None
    canonical_payload = canonical_terminal.model_dump(mode="json")
    if topic:
        canonical_payload["terminal_topic"] = topic
    error_message = str(canonical_payload.get("failure_reason") or "") or None
    raw_binding = canonical_payload.get("execution_binding")
    terminal_status = "failed" if is_failed else "completed"
    if raw_binding is not None:
        binding = parse_canonical_execution_binding(raw_binding)
        # ``ModelDelegationFailed`` deliberately represents both a terminal
        # failure and a terminal timeout; Core distinguishes them on the
        # canonical binding rather than inventing a third terminal topic.
        terminal_status = binding.terminal_kind
    return ModelDispatchBusTerminalResult(
        correlation_id=correlation_id,
        status=terminal_status,
        payload=canonical_payload,
        error_message=error_message,
    )


def _validate_runtime_execution_binding(
    *,
    dispatch_policy: DispatchPolicy | None,
    backend_id: str | None,
    tenant_id: str | None,
    correlation_id: UUID,
    rendered_contract_sha256: str | None,
    status: str,
    result: dict[str, object],
) -> None:
    """Bind a canonical terminal to the original selected-policy request."""
    raw_binding = result.get("execution_binding")
    if not is_backend_pinned_single_attempt(dispatch_policy):
        if raw_binding is not None:
            raise ValueError("unrequested dispatch must not carry execution_binding")
        return
    if raw_binding is None:
        if _is_unbound_pinned_served_model_terminal(status=status, result=result):
            # The provider omitted its served identity.  Preserve the observable
            # failed terminal but do not synthesize a request receipt or a model
            # binding from routing intent.
            return
        raise ValueError("pinned runtime terminal requires execution_binding")
    binding = parse_canonical_execution_binding(raw_binding)
    if (
        binding.dispatch_policy != dispatch_policy
        or binding.backend_id != backend_id
        or binding.tenant_id != tenant_id
        or binding.correlation_id != correlation_id
        or binding.rendered_contract_sha256 != rendered_contract_sha256
        or binding.terminal_kind != status
        or binding.attempt_count != 1
        or binding.fallback_used
        or binding.judge_used
    ):
        raise ValueError("execution_binding does not match pinned runtime request")


def _add_pinned_attempt_receipt(
    *,
    dispatch_policy: DispatchPolicy | None,
    result: dict[str, object],
) -> None:
    """Derive the delegate-skill attempt view from Core's bound terminal.

    The runtime terminal is Core's authoritative typed result.  The public
    delegate-skill response additionally requires one concrete attempt record,
    so derive that view only after the canonical terminal has been validated;
    no adapter-owned result dictionary is trusted as an authority.
    """
    if not is_backend_pinned_single_attempt(dispatch_policy):
        return
    raw_binding = result.get("execution_binding")
    if raw_binding is None:
        if _is_unbound_pinned_served_model_terminal(
            status=str(result.get("status") or ""), result=result
        ):
            return
        raise ValueError("pinned runtime terminal requires execution_binding")
    binding = parse_canonical_execution_binding(raw_binding)
    quality_score = result.get("quality_score")
    result["attempts"] = [
        {
            "tier": str(result.get("cost_tier_name") or ""),
            "backend_id": binding.backend_id,
            "model_id": binding.served_model_id,
            "quality_gate_passed": bool(result.get("quality_passed", False)),
            "quality_score": quality_score,
            "error_message": str(result.get("failure_reason") or ""),
        }
    ]


def _is_unbound_pinned_served_model_terminal(
    *,
    status: str,
    result: dict[str, object],
) -> bool:
    """Recognize the sole policy terminal allowed to omit a Core binding."""
    return (
        status == "failed"
        and result.get("model_used") == "unknown"
        and result.get("failure_reason") == "pinned_served_model_id_required"
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
    "ProtocolVerifiedPinnedAuthorizationSource",
    "RuntimeDelegationDispatchPort",
    "load_runtime_delegation_dispatch_config",
]
