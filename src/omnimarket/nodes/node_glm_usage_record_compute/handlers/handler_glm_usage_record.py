# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One harness run on the GLM Coding Plan becomes a usage event and, on a cap refusal, a refusal event (OMN-20287).

``HandlerGlmUsageRecord.handle(ModelGlmUsageRecordRequest) -> ModelGlmUsageRecordResult`` (definition-B).

The credits come from the run's tokens, the model's declared rate and whether the run started in peak hours.
A run that spent nothing (refused on budget, harness unavailable, ended before a token was counted) is still
one usage event with zero credits, so the projection counts every call. A refusal is a 429 retry note or a
provider code the policy maps to a cap; the strongest cap the run met names the scope (week, then window,
then rate), and the event keeps how the run ended: retried through the refusal, or failed.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.enums.enum_glm_allowance import EnumGlmRefusalScope
from omnimarket.models.glm_allowance.glm_credit_math import (
    credits_for_usage,
    peak_factor,
    rate_for_model,
)
from omnimarket.models.glm_allowance.model_glm_events import (
    ModelGlmRefusalObserved,
    ModelGlmUsageRecorded,
)
from omnimarket.models.glm_allowance.model_glm_usage_record import ModelGlmUsageRecord
from omnimarket.nodes.node_glm_usage_record_compute.models.model_glm_usage_record import (
    ModelGlmUsageRecordRequest,
    ModelGlmUsageRecordResult,
)

_RATE_LIMITED_STATUS = 429
_STRENGTH = {
    EnumGlmRefusalScope.RATE: 0,
    EnumGlmRefusalScope.WINDOW: 1,
    EnumGlmRefusalScope.WEEK: 2,
}


class HandlerGlmUsageRecord:
    """Turn one run's facts into the events the liveness projection shows."""

    def handle(self, request: ModelGlmUsageRecordRequest) -> ModelGlmUsageRecordResult:
        policy, run = request.policy, request.run
        counted = ModelGlmUsageRecord(
            run_id=run.run_id,
            started_at=run.started_at,
            model=run.model,
            input_tokens=run.input_tokens,
            cached_input_tokens=run.cached_input_tokens,
            output_tokens=run.output_tokens,
            credits=None,
        )
        usage = ModelGlmUsageRecorded(
            run_id=run.run_id,
            provider_id=policy.provider_id,
            model=run.model,
            task_class=run.task_class,
            lane=run.lane,
            started_at=run.started_at,
            outcome=run.outcome,
            input_tokens=run.input_tokens,
            cached_input_tokens=run.cached_input_tokens,
            output_tokens=run.output_tokens,
            credits=credits_for_usage(policy, counted),
            peak_factor=peak_factor(policy, run.started_at),
            rated=rate_for_model(policy, run.model)[1],
        )

        mapped = {c.code: c.scope for c in policy.cap_codes}
        coded = [(code, mapped[code]) for code in run.provider_codes if code in mapped]
        rate_limited = [s for s in run.retry_statuses if s == _RATE_LIMITED_STATUS]
        refusal: ModelGlmRefusalObserved | None = None
        if coded or rate_limited:
            code, scope = max(
                coded,
                key=lambda c: _STRENGTH[c[1]],
                default=(None, EnumGlmRefusalScope.RATE),
            )
            refusal = ModelGlmRefusalObserved(
                run_id=run.run_id,
                provider_id=policy.provider_id,
                model=run.model,
                task_class=run.task_class,
                lane=run.lane,
                observed_at=run.started_at + timedelta(seconds=run.duration_s),
                scope=scope,
                http_status=_RATE_LIMITED_STATUS if rate_limited else None,
                provider_code=code,
                count=max(len(rate_limited), len(coded), 1),
                run_outcome=run.outcome,
            )
        return ModelGlmUsageRecordResult(usage=usage, refusal=refusal)


__all__ = ["HandlerGlmUsageRecord"]
