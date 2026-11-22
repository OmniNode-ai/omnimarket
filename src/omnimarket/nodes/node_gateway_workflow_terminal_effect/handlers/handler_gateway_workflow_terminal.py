# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Terminal-outcome writer for gateway workflows.

Reads one delegation terminal event (delegation-completed or delegation-failed, both the flat
ModelDelegationResult wire shape), validates it before any SQL with a fixed precedence, and asks
the store to apply it to the one gateway row of the event's tenant and correlation id. The topic
names and the dead-letter topic come from this node's contract.yaml, never from a literal here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Final, Literal
from uuid import UUID

import yaml

from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_delegation_terminal_envelope import (
    ModelDelegationTerminalEnvelope,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalDeadLetterReason,
    EnumGatewayTerminalOutcome,
    EnumGatewayTerminalStoreOutcome,
    ModelGatewayTerminalApplied,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_terminal_update import (
    ModelTerminalUpdate,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.protocols.protocol_gateway_terminal_store import (
    ProtocolGatewayTerminalDeadLetter,
    ProtocolGatewayTerminalStore,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH: Final = Path(__file__).resolve().parents[1] / "contract.yaml"
_INT_MAX: Final = 2_147_483_647
_CREDENTIAL_SOURCES: Final = frozenset({"customer_key", "house", "none"})
# The retired writer's grammar for the machine-readable half: "<Class>: <CODE>".
_FAILURE_GRAMMAR: Final = re.compile(
    r"^(?P<cls>[A-Za-z_][A-Za-z0-9_.]*): (?P<code>[A-Za-z0-9_.\-]+)"
)
_OPERATION_STATUS: Final[dict[str, Literal["completed", "failed"]]] = {
    "apply_delegation_completed": "completed",
    "apply_delegation_failed": "failed",
}
# The only field names a dead-letter record may name: the wire model's own, never a value.
WIRE_FIELDS: Final = frozenset(
    {
        "correlation_id",
        "model_used",
        "route",
        "provider",
        "credential_source",
        "content",
        "latency_ms",
        "total_tokens",
        "failure_reason",
        "terminal_failure_reason",
        "rule_evaluations",
        "tenant_id",
    }
)


class _MalformedError(Exception):
    def __init__(self, reason: EnumGatewayTerminalDeadLetterReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


def load_contract_routes(
    contract_path: Path | None = None,
) -> tuple[dict[str, Literal["completed", "failed"]], str]:
    """Return ({topic: status}, dead-letter topic) as the contract declares them."""
    document = yaml.safe_load((contract_path or _CONTRACT_PATH).read_text("utf-8"))
    routes: dict[str, Literal["completed", "failed"]] = {}
    for entry in document["handler_routing"]["handlers"]:
        routes[str(entry["topic"])] = _OPERATION_STATUS[str(entry["operation"])]
    return routes, str(document["dlq_topics"][0])


def _as_uuid(value: Any) -> UUID:
    if not isinstance(value, str):
        raise _MalformedError(EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD)
    try:
        return UUID(value)
    except ValueError:
        raise _MalformedError(
            EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD
        ) from None


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _MalformedError(EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD)
    return value


def _count(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= _INT_MAX
    ):
        raise _MalformedError(EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD)
    return value


class HandlerGatewayWorkflowTerminal:
    """Write one delegation terminal onto its gateway workflow row."""

    def __init__(
        self,
        store: ProtocolGatewayTerminalStore | None = None,
        dead_letter: ProtocolGatewayTerminalDeadLetter | None = None,
        *,
        contract_path: Path | None = None,
    ) -> None:
        # The runtime builds an in-process handler with no arguments; the ports are then built
        # from the node's overlay document on the first delivery.
        self._store = store
        self._dead_letter = dead_letter
        self._routes, self._dlq_topic = load_contract_routes(contract_path)
        self.counters: dict[str, int] = {}

    def _count_event(self, name: str) -> None:
        self.counters[name] = self.counters.get(name, 0) + 1

    def _ports(
        self,
    ) -> tuple[ProtocolGatewayTerminalStore, ProtocolGatewayTerminalDeadLetter]:
        if self._store is None or self._dead_letter is None:
            from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.wiring_overlay import (
                build_ports_from_overlay,
            )

            self._store, self._dead_letter = build_ports_from_overlay()
        return self._store, self._dead_letter

    def _status_of(
        self, request: ModelDelegationTerminalEnvelope
    ) -> Literal["completed", "failed"] | None:
        if request.topic in self._routes:
            return self._routes[request.topic]
        # An event type such as "<family>.delegation-completed" names the same delivery as
        # the topic "<prefix>.<family>.delegation-completed.<version>".
        for topic, status in self._routes.items():
            parts = topic.split(".")
            if request.event_type and ".".join(parts[2:-1]) == request.event_type:
                return status
        return None

    async def handle(
        self, request: ModelDelegationTerminalEnvelope
    ) -> ModelGatewayTerminalApplied:
        status = self._status_of(request)
        if status is None:
            raise ValueError(
                f"delivery {request.topic or request.event_type!r} is not declared by the contract"
            )
        store, _ = self._ports()
        payload = request.payload
        try:
            update = self._validate(payload, status)
        except _MalformedError as exc:
            await self._publish_dead_letter(request, exc.reason)
            return ModelGatewayTerminalApplied(
                outcome=EnumGatewayTerminalOutcome.DEAD_LETTERED,
                dead_letter_reason=exc.reason,
            )
        if update is None:
            # No tenant on the wire: cannot be attributed to a tenant's row.
            self._count_event("terminal_unattributed")
            return ModelGatewayTerminalApplied(
                outcome=EnumGatewayTerminalOutcome.NOT_A_GATEWAY_WORKFLOW
            )
        outcome, workflow_id = await store.apply_terminal(update)
        if outcome is EnumGatewayTerminalStoreOutcome.AMBIGUOUS:
            reason = EnumGatewayTerminalDeadLetterReason.AMBIGUOUS_CORRELATION
            await self._publish_dead_letter(request, reason)
            return ModelGatewayTerminalApplied(
                outcome=EnumGatewayTerminalOutcome.DEAD_LETTERED,
                dead_letter_reason=reason,
            )
        if outcome is EnumGatewayTerminalStoreOutcome.NO_ROW:
            self._count_event("not_a_gateway_workflow")
            return ModelGatewayTerminalApplied(
                outcome=EnumGatewayTerminalOutcome.NOT_A_GATEWAY_WORKFLOW
            )
        if outcome is EnumGatewayTerminalStoreOutcome.ALREADY_TERMINAL:
            self._count_event("already_terminal")
            return ModelGatewayTerminalApplied(
                outcome=EnumGatewayTerminalOutcome.ALREADY_TERMINAL,
                workflow_id=workflow_id,
            )
        self._count_event("applied")
        return ModelGatewayTerminalApplied(
            outcome=EnumGatewayTerminalOutcome.APPLIED, workflow_id=workflow_id
        )

    def _validate(
        self, payload: dict[str, Any], status: Literal["completed", "failed"]
    ) -> ModelTerminalUpdate | None:
        # Precedence: an unreadable correlation or tenant id, then a missing model name,
        # then bad metrics, a route/provider mismatch or a credential source outside its vocabulary.
        correlation_id = _as_uuid(payload.get("correlation_id"))
        raw_tenant = payload.get("tenant_id")
        tenant_id = None if raw_tenant is None else _as_uuid(raw_tenant)
        model = payload.get("model_used")
        if not isinstance(model, str) or not model.strip():
            raise _MalformedError(
                EnumGatewayTerminalDeadLetterReason.MISSING_MODEL_USED
            )
        total_tokens = _count(payload, "total_tokens")
        latency_ms = _count(payload, "latency_ms")
        route = _optional_text(payload, "route") or None
        provider = _optional_text(payload, "provider") or None
        if (route is None) != (provider is None):
            raise _MalformedError(EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD)
        credential = _optional_text(payload, "credential_source") or None
        if credential is not None and credential not in _CREDENTIAL_SOURCES:
            raise _MalformedError(EnumGatewayTerminalDeadLetterReason.MALFORMED_PAYLOAD)
        content = _optional_text(payload, "content")
        failure_reason = _optional_text(payload, "failure_reason") or None
        machine_reason = _optional_text(payload, "terminal_failure_reason")
        failure_class = failure_code = None
        if machine_reason:
            matched = _FAILURE_GRAMMAR.match(machine_reason)
            if matched is not None:
                failure_class, failure_code = matched["cls"], matched["code"]
        evaluations = payload.get("rule_evaluations")
        if evaluations is not None and not isinstance(evaluations, list):
            self._count_event("terminal_rule_evaluations_dropped")
            evaluations = []
        if tenant_id is None:
            return None
        return ModelTerminalUpdate(
            tenant_id=tenant_id,
            correlation_id=correlation_id,
            status=status,
            model_used=model,
            total_tokens=total_tokens,
            latency_ms=latency_ms,
            route=route,
            provider=provider,
            credential_source=credential,
            content=content,
            failure_reason=failure_reason,
            failure_class=failure_class,
            failure_code=failure_code,
            rule_evaluations=list(evaluations or []),
        )

    async def _publish_dead_letter(
        self,
        request: ModelDelegationTerminalEnvelope,
        reason: EnumGatewayTerminalDeadLetterReason,
    ) -> None:
        payload = request.payload
        record: dict[str, Any] = {
            "reason": reason.value,
            "source_topic": request.topic or request.event_type,
            "fields_present": sorted(WIRE_FIELDS.intersection(payload)),
        }
        for key in ("correlation_id", "tenant_id"):
            try:
                record[key] = str(_as_uuid(payload.get(key)))
            except _MalformedError:
                continue
        _, dead_letter = self._ports()
        await dead_letter.publish(
            topic=self._dlq_topic,
            key=str(record.get("correlation_id", "unknown")),
            record=record,
        )
        self._count_event(f"dead_lettered_{reason.value}")


__all__: list[str] = ["HandlerGatewayWorkflowTerminal", "load_contract_routes"]
