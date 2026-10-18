# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Agent refusals of the omniclaude agent-model guard, as admission checks.

Probe row P4a showed that the classic PreToolUse agent-model guard never runs
for an Agent call the binding answers, so admission must refuse what the guard
refuses (OMN-21097). Ported from ``check_agent_input``, ``_route_problem`` and
``_score_band_rank`` in omniclaude ``plugins/onex/hooks/lib/workflow_model_guard.py``
at origin/dev; the bands and hard floors are contract data in this node's
``admission_policy``. The fork refusal is not here: admission refuses a fork
earlier as ``unsupported_path``.
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import JsonValue

from omnimarket.nodes.node_delegation_admission_compute.models.model_delegation_admission import (
    EnumAdmissionReason,
    ModelDelegationAdmissionPolicy,
)

_ROUTE_MARKER: Final[str] = "ROUTE:"

# Copied unchanged from the guard's _ROUTE_RE.
_ROUTE_RE: Final[re.Pattern[str]] = re.compile(
    r"ROUTE: band=(?P<band>B1c|B[0-5]) score=(?P<score>\d+) "
    r"dims=S(?P<S>[0-3])A(?P<A>[0-3])R(?P<R>[0-3])V(?P<V>[0-3])"
    r"N(?P<N>[0-3])C(?P<C>[0-3])J(?P<J>[0-3]) "
    r"dest=(?P<dest>[^\s\\]+) model=(?P<model>[A-Za-z0-9_.-]+) "
    r"effort=(?P<effort>[A-Za-z0-9_.-]+)"
)

_DIMENSION_KEYS: Final[tuple[str, ...]] = tuple("SARVNCJ")

#: B1c is a typed-choice variant of B1, so it ranks with it.
_BAND_RANK: Final[dict[str, int]] = {
    "B0": 0,
    "B1": 1,
    "B1c": 1,
    "B2": 2,
    "B3": 3,
    "B4": 4,
    "B5": 5,
}


_HAIKU_REMOVED_NOTE: Final[str] = (
    "haiku is removed from the background-agent allowlist (OMN-17427); "
    "trivial text work goes to `onex delegate` (local lab models) instead"
)


def _removed_note(value: str) -> str:
    """Name the haiku removal when the refused value is a haiku spelling."""
    return f"; {_HAIKU_REMOVED_NOTE}" if "haiku" in value.lower() else ""


def _allowed(values: frozenset[str] | tuple[str, ...]) -> str:
    return ", ".join(sorted(values))


def _score_band_rank(policy: ModelDelegationAdmissionPolicy, score: int) -> int:
    """Rank of the band whose score range holds ``score`` (0 when none does)."""
    for name, band in policy.route_bands.items():
        if (
            band.score_min is not None
            and band.score_max is not None
            and band.score_min <= score <= band.score_max
            and name != "B1c"
        ):
            return _BAND_RANK[name]
    return 0


def guard_refusal(
    tool_input: dict[str, JsonValue],
    policy: ModelDelegationAdmissionPolicy,
) -> tuple[EnumAdmissionReason, str] | None:
    """The guard's refusal for one Agent call, or ``None`` when it would allow it."""
    model = tool_input.get("model")
    if not isinstance(model, str) or not model.strip():
        return (
            EnumAdmissionReason.GUARD_NO_MODEL,
            "the Agent call declares no model, so it inherits the parent "
            "session's model",
        )
    declared = model.strip()
    if declared not in policy.allowed_models:
        return (
            EnumAdmissionReason.GUARD_MODEL_NOT_ALLOWED,
            f"model {declared!r} is not an allowed background model "
            f"(allowed: {_allowed(policy.allowed_models)})"
            f"{_removed_note(model)}",
        )
    if not policy.route_required:
        return None
    route_text = "\n".join(
        value
        for value in (tool_input.get("prompt"), tool_input.get("description"))
        if isinstance(value, str)
    )
    start = route_text.find(_ROUTE_MARKER)
    if start == -1:
        return (
            EnumAdmissionReason.GUARD_ROUTE_MISSING,
            "the ROUTE line is missing, so the task's complexity band was never "
            "scored; the model choice is unjustified",
        )
    match = _ROUTE_RE.match(route_text, start)
    if match is None:
        return (
            EnumAdmissionReason.GUARD_ROUTE_UNPARSABLE,
            "the ROUTE line is unparsable (expected 'ROUTE: band=<B> score=<n> "
            "dims=S<s>A<a>R<r>V<v>N<n>C<c>J<j> dest=<d> model=<m> effort=<e>')",
        )
    band_name = match["band"]
    score = int(match["score"])
    dims = {key: int(match[key]) for key in _DIMENSION_KEYS}
    if score != sum(dims.values()):
        return (
            EnumAdmissionReason.GUARD_ROUTE_SCORE_MISMATCH,
            f"ROUTE score={score} is not the sum of its dims ({sum(dims.values())})",
        )
    route_model = match["model"]
    if route_model != declared:
        return (
            EnumAdmissionReason.GUARD_ROUTE_MODEL_MISMATCH,
            f"ROUTE model={route_model} does not match declared model {declared!r}",
        )
    band = policy.route_bands[band_name]
    rank = _BAND_RANK[band_name]
    for floor in policy.route_hard_floors:
        if dims[floor.dimension] >= floor.value and rank < _BAND_RANK[floor.min_band]:
            return (
                EnumAdmissionReason.GUARD_ROUTE_HARD_FLOOR,
                f"hard floor: {floor.dimension}={dims[floor.dimension]} needs "
                f"band {floor.min_band} or above, but ROUTE says band {band_name}",
            )
    if rank < _score_band_rank(policy, score):
        return (
            EnumAdmissionReason.GUARD_ROUTE_BELOW_SCORE,
            f"band {band_name} is below the band score {score} implies; "
            "a task is never routed beneath its score",
        )
    if not band.models:
        return (
            EnumAdmissionReason.GUARD_ROUTE_BAND_NO_AGENT,
            f"band {band_name} dispatches no Claude agent (destination: "
            f"{band.destination}); an agent() or Agent call needs band B2 to B5",
        )
    if route_model not in band.models:
        return (
            EnumAdmissionReason.GUARD_ROUTE_BAND_MODEL,
            f"band {band_name} allows only: {_allowed(band.models)}; "
            f"ROUTE model {route_model!r} is not in that set",
        )
    if match["effort"] not in band.efforts:
        return (
            EnumAdmissionReason.GUARD_ROUTE_BAND_EFFORT,
            f"ROUTE effort={match['effort']} is not allowed for band "
            f"{band_name} (allowed: {_allowed(band.efforts)})",
        )
    return None


__all__ = ["guard_refusal"]
