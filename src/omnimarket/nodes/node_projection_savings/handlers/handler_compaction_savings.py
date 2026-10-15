# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compaction counted as token savings, beside delegation savings (OMN-18531).

A Claude Code compaction is a model call on the session's own model: the model
reads the whole context and writes a summary. When the compaction mod answers a
compaction itself, that call never happens. When the mod shortens what the
model reads, the call is smaller. The saving is what the model compaction would
have cost minus what it did cost, in tokens and in dollars at the session
model's manifest price:

    tokens saved = (context_tokens + counterfactual_output_tokens)
                 - (model_input_tokens + model_output_tokens)

* mod-answered: no model call, so ``model_input_tokens`` and
  ``model_output_tokens`` are 0, and the saving is the context the model would
  have read plus the summary it would have written.
* mod-shortened: the model read ``model_input_tokens`` instead of
  ``context_tokens``, and the saving is the difference.
* native (no mod): the model read the context and wrote the summary, so the
  two sides are equal and the saving is zero. No row is written: a compaction
  with no mod counts as zero savings, the way a delegation that banked nothing
  writes no row.

The counterfactual summary is counted only when it was measured. A
mod-answered compaction has no native summary to measure, so unless the
producer states one, the saving is the context half alone: a measured lower
bound, never a guessed total.

This is pure computation. ``SavingsProjectionRunner`` reads the source off the
PostCompact hook event and upserts the row this returns.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.pricing import build_premium_counterfactual

#: ``savings_estimates.task_type`` of every compaction row: the context service
#: plan's task class for compaction. It is what tells a compaction saving from a
#: delegation saving on every surface that reads the table.
COMPACTION_TASK_TYPE = "context_compaction"

#: ``savings_estimates.model_local`` of a compaction the mod answered: no model
#: served it.
COMPACTION_MOD_MODEL = "context-compaction-mod"

#: The hook event the source is read from. PreCompact does not fire when the mod
#: answers a compaction; PostCompact fires either way (capability probe, P6).
POST_COMPACT_HOOK_EVENT = "PostCompact"


class EnumCompactionOutcome(StrEnum):
    """Who did the compaction."""

    NATIVE = "native"
    MOD_ANSWERED = "mod_answered"
    MOD_SHORTENED = "mod_shortened"


class EnumCompactionSavingsSkip(StrEnum):
    """Why a compaction wrote no savings row. Each is a valid state, not an error."""

    NO_SAVING = "no_saving"
    CONTEXT_SIZE_NOT_CAPTURED = "context_size_not_captured"
    SESSION_MODEL_NOT_CAPTURED = "session_model_not_captured"
    SESSION_MODEL_UNPRICED = "session_model_unpriced"


class ModelCompactionSavingsSource(BaseModel):
    """One compaction, as the PostCompact hook event and the captured context state it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The PostCompact event's id: the identity of this one compaction. A Claude
    #: session compacts many times, so the session id cannot be the row key.
    compaction_id: str = Field(min_length=1)
    claude_session_id: str = Field(min_length=1)
    event_timestamp: AwareDatetime
    trigger: str | None = None
    outcome: EnumCompactionOutcome
    #: The session's model: the model a native compaction runs on.
    session_model: str | None = None
    #: The context the model compaction would have read, in tokens
    #: (``compactMetadata.preTokens`` of the boundary; for a shortened
    #: compaction, the context before the mod shortened it). None when the
    #: context size was not captured.
    context_tokens: int | None = Field(default=None, ge=0)
    #: What the compaction model actually read and wrote. Both 0 when the mod
    #: answered.
    model_input_tokens: int = Field(default=0, ge=0)
    model_output_tokens: int = Field(default=0, ge=0)
    #: The summary a native compaction would have written. Counted only when
    #: measured; None leaves it out of the saving.
    counterfactual_output_tokens: int | None = Field(default=None, ge=0)
    host: str | None = None


class ModelCompactionSavingsResult(BaseModel):
    """The saving of one compaction, or why there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    compaction_id: str
    outcome: EnumCompactionOutcome
    tokens_saved: int
    #: ``savings_estimates`` columns; None when no row is written.
    row: dict[str, Any] | None
    skipped: EnumCompactionSavingsSkip | None = None


class HandlerCompactionSavings:
    """Compute one compaction's saving: the measure, with no I/O."""

    def handle(
        self, request: ModelCompactionSavingsSource
    ) -> ModelCompactionSavingsResult:
        if request.outcome is EnumCompactionOutcome.NATIVE:
            return self._skip(request, 0, EnumCompactionSavingsSkip.NO_SAVING)
        if request.context_tokens is None:
            return self._skip(
                request, 0, EnumCompactionSavingsSkip.CONTEXT_SIZE_NOT_CAPTURED
            )

        counterfactual_output = request.counterfactual_output_tokens or 0
        baseline_in = request.context_tokens
        tokens_saved = (baseline_in + counterfactual_output) - (
            request.model_input_tokens + request.model_output_tokens
        )
        if tokens_saved <= 0:
            return self._skip(request, 0, EnumCompactionSavingsSkip.NO_SAVING)
        session_model = (request.session_model or "").strip()
        if not session_model:
            return self._skip(
                request,
                tokens_saved,
                EnumCompactionSavingsSkip.SESSION_MODEL_NOT_CAPTURED,
            )

        baseline = build_premium_counterfactual(
            prompt_tokens=baseline_in,
            completion_tokens=counterfactual_output,
            premium_model=session_model,
            measured=True,
        )
        actual = build_premium_counterfactual(
            prompt_tokens=request.model_input_tokens,
            completion_tokens=request.model_output_tokens,
            premium_model=session_model,
            measured=True,
        )
        if baseline is None or actual is None:
            return self._skip(
                request, tokens_saved, EnumCompactionSavingsSkip.SESSION_MODEL_UNPRICED
            )

        cloud_cost_usd = _usd(baseline.counterfactual_cost_usd)
        local_cost_usd = _usd(actual.counterfactual_cost_usd)
        model_local = (
            COMPACTION_MOD_MODEL
            if request.outcome is EnumCompactionOutcome.MOD_ANSWERED
            else session_model
        )
        row: dict[str, Any] = {
            "session_id": request.compaction_id,
            "event_timestamp": request.event_timestamp,
            "model_local": model_local,
            "model_cloud_baseline": session_model,
            "local_cost_usd": local_cost_usd,
            "cloud_cost_usd": cloud_cost_usd,
            "savings_usd": cloud_cost_usd - local_cost_usd,
            "repo_name": None,
            "machine_id": request.host,
            "task_type": COMPACTION_TASK_TYPE,
            # The tokens the model compaction would have spent, which is what
            # the saving is counted from.
            "prompt_tokens": baseline_in,
            "completion_tokens": counterfactual_output,
            # Every counted token is measured: the counterfactual summary is
            # left out when it was not.
            "savings_method": "measured",
            "usage_source": "measured",
        }
        return ModelCompactionSavingsResult(
            compaction_id=request.compaction_id,
            outcome=request.outcome,
            tokens_saved=tokens_saved,
            row=row,
        )

    @staticmethod
    def _skip(
        request: ModelCompactionSavingsSource,
        tokens_saved: int,
        reason: EnumCompactionSavingsSkip,
    ) -> ModelCompactionSavingsResult:
        return ModelCompactionSavingsResult(
            compaction_id=request.compaction_id,
            outcome=request.outcome,
            tokens_saved=tokens_saved,
            row=None,
            skipped=reason,
        )


def compaction_source_from_post_compact(
    data: dict[str, Any],
) -> ModelCompactionSavingsSource | None:
    """Read a compaction source off one ``hook-event.v1`` record.

    None for any hook event that is not PostCompact. The outcome comes from the
    summary the harness reports: a native compaction writes a summary (9,623 to
    44,930 characters over the 54 captured in the two weeks to 2026-10-15), and
    a compaction the mod answered reports an empty one (capability probe, P6).
    The context size is ``payload.context_tokens`` and the session model is
    ``payload.model``, both declared capture keys; each is None until the
    producer stamps it on PostCompact.
    """
    if data.get("hook_event_name") != POST_COMPACT_HOOK_EVENT:
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    lineage = data.get("lineage")
    session_id = lineage.get("session_id") if isinstance(lineage, dict) else None
    emitted_at = data.get("emitted_at")
    event_timestamp = (
        datetime.fromisoformat(emitted_at)
        if isinstance(emitted_at, str)
        else emitted_at
    )
    # The producer always states the summary's reference (omniclaude
    # ModelPostCompactPayload requires it), so only a stated length of 0 reads
    # as the mod's answer. A missing reference is not evidence the mod answered
    # and reads as native: zero, never a saving.
    summary_ref = payload.get("compact_summary_ref")
    summary_chars = summary_ref.get("length") if isinstance(summary_ref, dict) else None
    outcome = (
        EnumCompactionOutcome.MOD_ANSWERED
        if summary_chars == 0
        else EnumCompactionOutcome.NATIVE
    )
    model = payload.get("model")
    context_tokens = payload.get("context_tokens")
    return ModelCompactionSavingsSource(
        compaction_id=str(data.get("event_id") or ""),
        claude_session_id=str(session_id or ""),
        event_timestamp=event_timestamp,
        trigger=payload.get("trigger")
        if isinstance(payload.get("trigger"), str)
        else None,
        outcome=outcome,
        session_model=model if isinstance(model, str) else None,
        context_tokens=context_tokens if isinstance(context_tokens, int) else None,
        host=data.get("host") if isinstance(data.get("host"), str) else None,
    )


def _usd(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))
