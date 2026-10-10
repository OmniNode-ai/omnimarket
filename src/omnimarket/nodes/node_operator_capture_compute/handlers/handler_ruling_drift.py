# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRulingDrift: is this decision a re-ruling of a settled subject? (OMN-20907).

Pure and deterministic. A captured decision or preference is compared with every earlier RULING
row and every earlier captured decision or preference. When the item's subject terms are mostly
present in an earlier one, the earlier stamp is returned: the operator is ruling again on a
matter the process should already have carried, and the capture row says so (drift=re-ruled)
instead of reading as a new decision.

The relation is a polarity check, not a judgement: ``contradicts`` when exactly one of the two
texts is negated (no, not, never, don't, stop, without), otherwise ``reaffirms``. Both are drift;
a contradiction is the louder one because it changes a standing rule, possibly by accident.
"""

from __future__ import annotations

import re
from datetime import UTC

from omnimarket.models.operator_capture import (
    EnumDriftRelation,
    ModelDriftMatch,
    ModelPriorRuling,
    ModelRulingDrift,
    ModelRulingDriftRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
    subject_terms,
)

MIN_SHARED_TERMS = 2
MIN_SHARED_FRACTION = 0.6
MAX_MATCHES = 5
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([A-Z][A-Z-]*) \| (.*)$")
_QUOTED = re.compile(r'"([^"]{3,})"')
_NEGATION = re.compile(
    r"\b(?:no|not|never|don't|dont|do not|shouldn't|should not|stop|without|none|nothing|"
    r"isn't|aren't|won't|cannot|can't)\b",
    re.IGNORECASE,
)


def _cells(rest: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in rest.split(" | "):
        key, sep, value = cell.partition("=")
        if sep and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key) and key not in cells:
            cells[key] = value.strip()
    return cells


def prior_rulings_from_rows(
    rows: tuple[str, ...] | list[str],
) -> tuple[ModelPriorRuling, ...]:
    """Every RULING row and every captured decision or preference, oldest first."""
    prior: list[ModelPriorRuling] = []
    for row in rows:
        match = _ROW.match(row.rstrip("\n"))
        if not match:
            continue
        stamp, kind, rest = match.groups()
        cells = _cells(rest)
        lane = cells.get("lane", "")
        if kind == "RULING":
            words = " ".join(_QUOTED.findall(rest))
            text = f"{cells.get('question', '')} {words}".strip()
        elif (
            kind == "STATUS"
            and cells.get("kind") in {"decision", "preference"}
            and ("item" in cells and "classifier" in cells)
        ):
            text = " ".join(_QUOTED.findall(rest))
        else:
            continue
        if text:
            prior.append(ModelPriorRuling(stamp=stamp, lane=lane, text=text))
    return tuple(prior)


def _terms(text: str) -> set[str]:
    return set(subject_terms(text, 10_000))


class HandlerRulingDrift:
    """Name the earlier rulings a decision re-rules, newest first."""

    def handle(self, request: ModelRulingDriftRequest) -> ModelRulingDrift:
        item = request.item
        # Two vocabularies, because a model's subject is often abstract ("cloud work milestone
        # allocation") while the quote is concrete ("no cloud work in M4"): a prior ruling
        # matches when it carries most of either.
        vocabularies = [
            v
            for v in (
                set(subject_terms(item.subject, 6)),
                set(subject_terms(item.quote, 8)),
            )
            if len(v) >= MIN_SHARED_TERMS
        ]
        if not vocabularies:
            return ModelRulingDrift()
        quote_terms = set(subject_terms(item.quote, 8))
        negated = bool(_NEGATION.search(item.quote))
        cutoff = (
            request.said_at.astimezone(UTC).strftime(_STAMP_FORMAT)
            if request.said_at
            else None
        )
        matches: list[ModelDriftMatch] = []
        for prior in reversed(request.prior):
            if prior.text.strip() == item.quote.strip():
                continue
            if cutoff is not None and prior.stamp >= cutoff:
                continue
            prior_terms = _terms(prior.text)
            shared: set[str] = set()
            for vocabulary in vocabularies:
                overlap = vocabulary & prior_terms
                if (
                    len(overlap) >= MIN_SHARED_TERMS
                    and len(overlap) / len(vocabulary) >= MIN_SHARED_FRACTION
                    and len(overlap) > len(shared)
                ):
                    shared = overlap
            # Grounding: the operator's own words must share the terms too, so an abstract model
            # subject ("fixing things confirmation") cannot match on its own vocabulary alone.
            if not shared or len(quote_terms & prior_terms) < MIN_SHARED_TERMS:
                continue
            relation = (
                EnumDriftRelation.CONTRADICTS
                if negated != bool(_NEGATION.search(prior.text))
                else EnumDriftRelation.REAFFIRMS
            )
            matches.append(
                ModelDriftMatch(
                    stamp=prior.stamp,
                    lane=prior.lane,
                    relation=relation,
                    shared_terms=tuple(sorted(shared)),
                )
            )
            if len(matches) >= MAX_MATCHES:
                break
        return ModelRulingDrift(matches=tuple(matches))


__all__ = ["HandlerRulingDrift", "prior_rulings_from_rows"]
