# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Choose which read served a lab-fill tick's candidates (OMN-20668).

The enumerate read refuses a cached list built before the newest lab-fill claim: the
cache is then not used, its candidates and fences are dropped, and the tick says so
(cache-stale, detail older-than-claim). When the live read printed no JSON and the
cache-only read served the tick, the diagnostics, prefiltered skips and filtered
counts are the cache read's, never the empty live read's.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..models import (
    ModelLabFillCandidateChoiceRequest,
    ModelLabFillCandidateChoiceResult,
)
from .handler_lab_fill_dispatch_plan import APPROVED_KINDS
from .helpers_js_value import get, is_finite_number, round_half_up, text_of


def _candidates_of(read: Mapping[str, object] | None) -> list[object] | None:
    """The read's candidate list, or None when it carries no list."""
    candidates = get(read, "candidates")
    return list(candidates) if isinstance(candidates, list) else None


def _usable(cached: Mapping[str, object] | None, fallback_max_age_min: int) -> bool:
    age = get(cached, "age_min")
    return (
        _candidates_of(cached) is not None
        and isinstance(age, int | float)
        and is_finite_number(age)
        and float(age) <= fallback_max_age_min
    )


def _fences(result: Mapping[str, object] | None) -> list[str]:
    fenced = get(result, "fenced_tickets")
    return list(fenced) if isinstance(fenced, list) else []


def _object(result: Mapping[str, object] | None, key: str) -> object | None:
    value = get(result, key)
    return value if isinstance(value, dict) else None


class HandlerLabFillCandidateChoice:
    """Pick the live read, the cache-only read, or both, and carry what they printed."""

    def handle(
        self, request: ModelLabFillCandidateChoiceRequest
    ) -> ModelLabFillCandidateChoiceResult:
        enumerated, cached = request.enumerated, request.cached
        live = _candidates_of(enumerated)
        # The open-count gate the read decided: the live read's, else the cache-only read's.
        gate = _object(enumerated, "open_count_gate") or _object(
            cached, "open_count_gate"
        )
        rejected = get(enumerated, "cache_rejected")
        if (
            live is not None
            and isinstance(rejected, str)
            and rejected != ""
            and not any(get(c, "kind") in APPROVED_KINDS for c in live)
        ):
            stale: dict[str, object] = {}
            diagnostics_of = _object(enumerated, "source_diagnostics")
            if diagnostics_of is not None:
                stale["source_diagnostics"] = diagnostics_of
            if gate is not None:
                stale["open_count_gate"] = gate
            return ModelLabFillCandidateChoiceResult(
                source="cache-stale",
                candidates=(),
                fenced=(),
                detail="older-than-claim",
                **stale,
            )
        source = "none"
        candidates: list[object] = []
        fenced: list[str] = []
        cached_candidates = _candidates_of(cached)
        usable = _usable(cached, request.config.fallback_max_age_min)
        if live is not None:
            candidates = live
            fenced = _fences(enumerated)
            if get(enumerated, "truncated") is not True:
                source = (
                    "enumerate-cache"
                    if text_of(get(enumerated, "source")).startswith("cache")
                    else "enumerate"
                )
            elif usable and cached_candidates is not None:
                source = "enumerate+cache"
                tickets = {get(c, "ticket") for c in candidates}
                for c in cached_candidates:
                    ticket = get(c, "ticket")
                    if ticket not in tickets:
                        candidates.append(c)
                        tickets.add(ticket)
                fenced = list(dict.fromkeys([*fenced, *_fences(cached)]))
            else:
                source = "enumerate-partial"
        elif usable and cached_candidates is not None:
            source = "cache"
            candidates = cached_candidates
            fenced = _fences(cached)
        cache_result = (
            cached
            if cached is not None
            else (enumerated if source == "enumerate-cache" else None)
        )
        age = get(cache_result, "age_min")
        detail = (
            f"age={int(round_half_up(float(age)))}m"
            if "cache" in source
            and isinstance(age, int | float)
            and is_finite_number(age)
            else ""
        )
        # The read that served: the cache-only read when it filled in for a truncated live read.
        served = (
            cached
            if source == "enumerate+cache"
            else (enumerated if live is not None else cached)
        )
        extra: dict[str, object] = {}
        filtered = _object(served, "filtered")
        if filtered is not None:
            extra["filtered"] = filtered
        if gate is not None:
            extra["open_count_gate"] = gate
        diagnostics = _object(served, "source_diagnostics")
        if diagnostics is not None:
            extra["source_diagnostics"] = diagnostics
        skipped = get(served, "skipped")
        if isinstance(skipped, list) and skipped:
            extra["prefiltered_skipped"] = tuple(skipped)
        not_emitted = _object(served, "not_emitted")
        if not_emitted:
            extra["not_emitted"] = not_emitted
        return ModelLabFillCandidateChoiceResult(
            source=source,
            candidates=tuple(candidates),
            fenced=tuple(fenced),
            detail=detail,
            **extra,
        )
