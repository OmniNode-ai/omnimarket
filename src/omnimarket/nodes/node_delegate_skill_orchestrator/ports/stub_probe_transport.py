# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scripted effect handler for probe-ladder runs (OMN-21212).

Each rung's endpoint is given one scripted reply: an answer, or a typed
failure such as a 429. The transport records every call it receives, so a
test can assert which rungs were asked, in which order, and that a refused
run asked none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)


@dataclass(frozen=True)
class StubProbeReply:
    """What one stub endpoint answers: ``content``, or a typed failure."""

    content: str = ""
    failure_class: EnumDelegationFailureClass | None = None
    http_status: int | None = None

    @classmethod
    def answer(cls, content: str) -> StubProbeReply:
        return cls(content=content)

    @classmethod
    def rate_limited(cls) -> StubProbeReply:
        return cls(
            failure_class=EnumDelegationFailureClass.RATE_LIMITED, http_status=429
        )


@dataclass
class StubProbeTransport:
    """An effect handler answering each endpoint with its scripted reply."""

    replies: dict[str, StubProbeReply]
    calls: list[str] = field(default_factory=list)

    def __call__(
        self, request: ModelLlmDelegationCallRequest
    ) -> ModelLlmDelegationCallResult:
        self.calls.append(request.endpoint_ref)
        reply = self.replies.get(request.endpoint_ref)
        if reply is None:
            raise AssertionError(
                f"stub probe transport has no reply for {request.endpoint_ref!r}"
            )
        failed = reply.failure_class is not None
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=not failed,
            content=reply.content,
            tokens_in=0 if failed else 11,
            tokens_out=0 if failed else 22,
            latency_ms=1,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
            http_status=reply.http_status,
            failure_class=reply.failure_class,
            error_message=(
                f"stub {reply.http_status or 'failure'}: {reply.failure_class.value}"
                if reply.failure_class is not None
                else None
            ),
        )


__all__ = ["StubProbeReply", "StubProbeTransport"]
