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
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusTerminalResult,
)
from omnimarket.events.delegation import ModelDelegationRequest
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
)

_DEFAULT_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_CONFIG_KEY = "delegation_runtime_dispatch"


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
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        # OMN-13161: the bus runtime path carries its own routing-tier budgets in
        # the downstream delegation chain. When the request omits max_tokens, fall
        # back to the runtime model's contract default rather than forcing a value;
        # the per-backend ceiling is applied on the in-process local path.
        max_tokens_fields: dict[str, int] = (
            {} if max_tokens is None else {"max_tokens": max_tokens}
        )
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
        )

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
        return result

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
    nested_payload = payload.get("payload")
    if isinstance(nested_payload, dict):
        flattened = dict(nested_payload)
        topic = payload.get("topic")
        if isinstance(topic, str) and topic:
            flattened["terminal_topic"] = topic
        return flattened
    return payload


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

    topic = str(envelope_payload.get("topic") or raw.get("event_type") or "")
    is_failed = topic == failed_topic or bool(terminal_payload.get("failure_reason"))
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
