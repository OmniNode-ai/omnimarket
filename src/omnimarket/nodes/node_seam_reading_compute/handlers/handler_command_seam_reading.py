# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify the command-publication seam, without inventing execution evidence."""

from datetime import timedelta

from omnimarket.models.model_command_seam import (
    ModelCommandSeamFact,
    ModelCommandSeamFacts,
    ModelCommandSeamReading,
    ModelCommandSeamReadings,
)


class HandlerCommandSeamReading:
    """Pure reader; loss of the sampler makes its last reading expire."""

    def handle(self, request: ModelCommandSeamFacts) -> ModelCommandSeamReadings:
        valid_until = request.sampled_at + timedelta(seconds=request.max_age_seconds)
        stale = not request.sampled_at <= request.read_at <= valid_until
        return ModelCommandSeamReadings(
            scope=request.scope,
            sample_id=request.sample_id,
            sampled_at=request.sampled_at,
            read_at=request.read_at,
            valid_until=valid_until,
            part_index=request.part_index,
            part_count=request.part_count,
            inventory_count=request.inventory_count,
            readings=tuple(self._read(fact, stale, request) for fact in request.facts),
            errors=request.errors,
        )

    def _read(
        self, fact: ModelCommandSeamFact, stale: bool, request: ModelCommandSeamFacts
    ) -> ModelCommandSeamReading:
        state: ModelCommandSeamReading
        if stale or request.errors or fact.binding_error or not fact.topic_observed:
            verdict = "UNKNOWN"
            reason = (
                "stale-or-future-sample"
                if stale
                else fact.binding_error
                or ("source-unreadable" if request.errors else "topic-not-observed")
            )
        elif (
            fact.invocation is not None
            and fact.invocation.automatic_required
            and not fact.automatic_senders
        ):
            verdict, reason = "NOT_WORKING", "required-automatic-trigger-unbound"
        elif not fact.automatic_senders:
            if fact.invocation is not None and fact.invocation.mode == "on_demand":
                verdict, reason = (
                    "ON_DEMAND_IDLE",
                    "explicit-on-demand-no-automatic-sender",
                )
            elif fact.external_sender_declarations:
                verdict, reason = "UNKNOWN", "external-sender-mode-unmeasured"
            else:
                verdict, reason = (
                    "NOT_WORKING",
                    "no-bound-automatic-trigger-or-invocation-policy",
                )
        elif fact.last_crossed_at is None:
            verdict, reason = (
                "UNKNOWN",
                "automatic-path-declared-last-crossing-unmeasured",
            )
        elif fact.last_crossed_source != "sampler":
            verdict, reason = "UNKNOWN", "historical-crossing-current-log-unmeasured"
        elif fact.last_crossed_at > request.sampled_at:
            verdict, reason = "UNKNOWN", "record-timestamp-after-sample"
        elif (
            request.sampled_at - fact.last_crossed_at
        ).total_seconds() > request.max_age_seconds:
            verdict, reason = "UNKNOWN", "no-recent-crossing-demand-unmeasured"
        else:
            verdict, reason = (
                "WORKING",
                "automatic-path-declared-and-command-publication-observed",
            )
        state = ModelCommandSeamReading(
            node=fact.node,
            package=fact.package,
            topic=fact.topic,
            state=verdict,
            reason=reason,
            automatic_senders=fact.automatic_senders,
            declared_sender=(
                fact.invocation.sender
                if fact.invocation
                else ", ".join(fact.external_sender_declarations) or None
            ),
            last_crossed_at=fact.last_crossed_at,
            last_crossed_source=fact.last_crossed_source,
            high_watermark=fact.high_watermark,
            low_watermark=fact.low_watermark,
        )
        return state
