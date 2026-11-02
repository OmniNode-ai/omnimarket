# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The TA.12 runner (OMN-18531 TA.12, OMN-17427).

Drives this node's adapter in process over recorded PreCompact hook inputs with a
compaction binding it supplies (the binding is not installed in Claude Code).
Cold trip = a fresh receipt-mode client from the factory for that trip, its
construction inside the timed span; warm trip
= one client from the factory shared by every warm trip of a case, after one
discarded warm-up trip.

The phase split is read from the prepared receipt the trip awaited:
deterministic_build_ms = build_ms - model_section_ms. A missing receipt, a missing
or invalid build_ms or model_section_ms, or a model section longer than the build
is a refusal, never a guessed number. Its trips are the raw input the latency
qualification compute tabulates; this module does not tabulate.

Recorded inputs carry the actual input_messages_hash and, when captured, the
newest_turn, barrier_id and compact_model. The replay never fabricates a hash,
barrier or model choice. An invalid command is refused before publication, and
only an install verdict can supply a successful phase measurement.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from pydantic import ValidationError

from omnimarket.models.claude_code_binding import ModelClientBinding
from omnimarket.models.context_compaction import (
    ModelContextCompactionRequested,
    load_reply_topic_templates,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_claude_code_hook_adapter import (
    INPUT_MESSAGES_HASH_SOURCE,
    SESSION_BARRIER_SOURCE,
    HandlerClaudeCodeHookAdapter,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.handler_client_bindings_loader import (
    CONTRACT_PATH,
    load_client_section,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.handlers.reply_principal import (
    ReplyPrincipalResolver,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    EnumCompactionLatencyTemperature,
    ModelCompactionLatencyCase,
    ModelCompactionLatencyRunRequest,
    ModelCompactionLatencyRunResult,
    ModelCompactionLatencyTrip,
    ModelHookAdapterRequest,
    ModelReceiptEvent,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.protocols import (
    ProtocolReceiptModeClient,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

PREPARED_BUILD_MS_KEY = "build_ms"
PREPARED_MODEL_SECTION_MS_KEY = "model_section_ms"
_ADAPTER_ERRORS = (OSError, ValueError, KeyError, TypeError, RuntimeError)


#: Replayed hook inputs carry the hash and barrier the installed hook derives.
_REPLAYED_SOURCES = {
    INPUT_MESSAGES_HASH_SOURCE: "$.input_messages_hash",
    SESSION_BARRIER_SOURCE: "$.barrier_id",
}


def compaction_latency_binding(*, deadline_ms: int) -> ModelClientBinding:
    """The shipped compaction binding, replayed as the plan's PreCompact binding.

    Its awaited per-principal reply topics and the arguments naming them are the
    shipped ones, so the replay awaits what the installed hook does (OMN-22103).
    """
    shipped = next(
        b for b in load_client_section(CONTRACT_PATH).bindings if b.name == "compaction"
    )
    return shipped.model_copy(
        update={
            "hook_event": "PreCompact",
            "matcher_tool": None,
            "deadline_ms": deadline_ms,
            "arguments": tuple(
                a.model_copy(
                    update={"source": _REPLAYED_SOURCES.get(a.source, a.source)}
                )
                for a in shipped.arguments
            ),
        }
    )


class ReceiptRecorder:
    """Validate the compaction command and retain the last call's awaited receipts."""

    def __init__(self, client: ProtocolReceiptModeClient) -> None:
        self._client = client
        self.last_events: tuple[ModelReceiptEvent, ...] = ()

    def publish_and_await(
        self,
        *,
        topic: str,
        command_type: str,
        payload: dict[str, JsonValue],
        awaited_events: tuple[str, ...],
        deadline_ms: int,
    ) -> tuple[ModelReceiptEvent, ...]:
        self.last_events = ()
        try:
            ModelContextCompactionRequested.model_validate(payload)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(part) for part in error["loc"]) for error in exc.errors()}
            )
            raise ValueError(
                f"invalid compaction command fields: {', '.join(fields)}"
            ) from exc
        self.last_events = self._client.publish_and_await(
            topic=topic,
            command_type=command_type,
            payload=payload,
            awaited_events=awaited_events,
            deadline_ms=deadline_ms,
        )
        return self.last_events


def split_prepared(events: Sequence[ModelReceiptEvent]) -> tuple[float, float] | str:
    """Split the last prepared receipt's build total into its two phases."""
    prepared_template = load_reply_topic_templates().prepared
    prepared = next(
        (
            event
            for event in reversed(events)
            if prepared_template.principal_of(event.topic) is not None
        ),
        None,
    )
    if prepared is None:
        return "no prepared receipt"
    phases: list[float] = []
    for key in (PREPARED_BUILD_MS_KEY, PREPARED_MODEL_SECTION_MS_KEY):
        value = prepared.payload.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return f"prepared receipt has no {key}"
        phases.append(float(value))
    build_ms, model_section_ms = phases
    if model_section_ms > build_ms:
        return "model_section_ms exceeds build_ms"
    return build_ms - model_section_ms, model_section_ms


class HandlerCompactionLatencyRun:
    """Drive cold trips, then warmed trips, in recorded case order."""

    def __init__(
        self,
        client_factory: Callable[[], ProtocolReceiptModeClient],
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._client_factory = client_factory
        self._clock = clock

    def _new_client(self) -> ReceiptRecorder | str:
        try:
            return ReceiptRecorder(self._client_factory())
        except _ADAPTER_ERRORS as exc:
            return f"adapter failed: {type(exc).__name__}: {exc}"

    def _trip(
        self,
        request: ModelCompactionLatencyRunRequest,
        case: ModelCompactionLatencyCase,
        temperature: EnumCompactionLatencyTemperature,
        index: int,
        client: ReceiptRecorder | str | None,
    ) -> ModelCompactionLatencyTrip:
        """Time one trip; a None client is built inside the timed span (a cold trip)."""
        adapter_request = ModelHookAdapterRequest(
            binding=compaction_latency_binding(deadline_ms=request.deadline_ms),
            hook_input=case.hook_input,
        )
        verdict_action: str | None = None
        split: tuple[float, float] | str
        started = self._clock()
        if client is None:
            client = self._new_client()
        try:
            if isinstance(client, str):
                split = client
            else:
                result = HandlerClaudeCodeHookAdapter(
                    client, principal_resolver=ReplyPrincipalResolver().resolve
                ).handle(adapter_request)
                verdict_action = result.client_verdict.action.value
                split = "no prepared receipt"
        except _ADAPTER_ERRORS as exc:
            split = f"adapter failed: {type(exc).__name__}: {exc}"
        finally:
            wall_ms = (self._clock() - started) * 1000
        if verdict_action is not None and isinstance(client, ReceiptRecorder):
            split = (
                split_prepared(client.last_events)
                if verdict_action == "install"
                else f"adapter verdict is {verdict_action}"
            )
        return ModelCompactionLatencyTrip(
            round_trip_id=f"{request.host}:{case.size}:{temperature.value}:{index}",
            size=case.size,
            host=request.host,
            temperature=temperature,
            wall_ms=wall_ms,
            deterministic_build_ms=None if isinstance(split, str) else split[0],
            model_section_ms=None if isinstance(split, str) else split[1],
            verdict_action=verdict_action,
            refusal=split if isinstance(split, str) else None,
        )

    def handle(
        self, request: ModelCompactionLatencyRunRequest
    ) -> ModelCompactionLatencyRunResult:
        trips: list[ModelCompactionLatencyTrip] = []
        for case in request.cases:
            for index in range(request.cold):
                trips.append(
                    self._trip(
                        request,
                        case,
                        EnumCompactionLatencyTemperature.COLD,
                        index,
                        None,
                    )
                )
            if request.warm:
                client = self._new_client()
                if isinstance(client, ReceiptRecorder):
                    self._trip(
                        request, case, EnumCompactionLatencyTemperature.WARM, -1, client
                    )
                for index in range(request.warm):
                    trips.append(
                        self._trip(
                            request,
                            case,
                            EnumCompactionLatencyTemperature.WARM,
                            index,
                            client,
                        )
                    )
        return ModelCompactionLatencyRunResult(
            host=request.host,
            trips=tuple(trips),
            refusal_count=sum(trip.refusal is not None for trip in trips),
        )
