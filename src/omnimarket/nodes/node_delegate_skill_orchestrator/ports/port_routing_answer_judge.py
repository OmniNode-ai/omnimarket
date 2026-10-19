# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The production independent-judge call for the answer gate (OMN-20910).

``node_delegation_answer_gate_compute`` is pure: the judge call arrives as a
value. This is the call. It posts the judge messages to the routing-contract
backend the gate selected, through ``RoutingResolvedJudgeInferenceAdapter`` (the
same endpoint, secret and transport resolution the adequacy judge uses), and
reads the verdict with the class's own reply shape.

The adapter falls back to a local rung when the declared judge's credential is
not bound on the machine. That rung is the producer's own family, so recording
its answer as the independent judge's would be a same-family judge under another
name. The call therefore refuses when the adapter resolved a model other than the
one the gate selected; the dispatch port records that as UNAVAILABLE, which the
gate refuses.
"""

from __future__ import annotations

import asyncio

from omnimarket.models.delegation.wire.model_answer_gate_request import (
    ModelAnswerJudgeCall,
    ModelIndependentJudgeVerdict,
)
from omnimarket.nodes.node_delegation_answer_gate_compute.handlers.judge_selection import (
    parse_judge_reply,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.adapter_routing_resolved_judge import (
    RoutingResolvedJudgeInferenceAdapter,
)

_JUDGE_TIMEOUT_SECONDS = 180.0
_JUDGE_TEMPERATURE = 0.0


async def routing_answer_judge(
    call: ModelAnswerJudgeCall, *, task_type: str | None = None
) -> ModelIndependentJudgeVerdict:
    """Ask the selected judge backend for its verdict on one answer."""
    adapter = RoutingResolvedJudgeInferenceAdapter(backend_id=call.backend_id)
    # Backend resolution reads the secret store; keep that blocking read off the loop.
    resolved = await asyncio.to_thread(adapter.resolved_model_id)
    if resolved != call.model_id:
        raise RuntimeError(
            f"resolved reviewer {resolved!r} is not the selected judge "
            f"{call.model_id!r} ({call.backend_id}); refusing a judge the gate did "
            "not select"
        )
    reply = await adapter.infer(
        model_key=resolved,
        system_prompt=call.system_prompt,
        user_prompt=call.user_prompt,
        timeout_seconds=_JUDGE_TIMEOUT_SECONDS,
        temperature=_JUDGE_TEMPERATURE,
    )
    verdict, reason = parse_judge_reply(reply, task_type=task_type)
    return ModelIndependentJudgeVerdict(
        backend_id=call.backend_id,
        model_id=call.model_id,
        verdict=verdict,
        reason=reason,
    )


__all__ = ["routing_answer_judge"]
