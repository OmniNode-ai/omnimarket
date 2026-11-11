# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Bound the size of every answer the work ledger read node publishes.

The completed topic carries the answer as one record and the broker refuses a
record over its ``max_request_size``. An answer over the contract's bound is
replaced by a typed refusal that names the bound, the size it would have been
and the bounded form of the same question, so the caller narrows the request
instead of retrying it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.models.work_ledger_query import (
    POINT_QUERY_KINDS,
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerAnswerBound,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRefusal,
)

#: Share of the bound a suggested ``limit`` is sized to, so the bounded form
#: does not sit exactly on the edge it was refused at.
_SUGGESTION_FILL = 0.8


def load_work_ledger_answer_bound(contract_path: Path) -> ModelWorkLedgerAnswerBound:
    """Load and validate the contract's declared answer bound."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    section = raw.get("work_ledger_query") if isinstance(raw, dict) else None
    bound = section.get("answer_bound") if isinstance(section, dict) else None
    if not isinstance(bound, dict):
        raise ValueError("work_ledger_query.answer_bound must be a mapping")
    return ModelWorkLedgerAnswerBound.model_validate(bound)


def answer_size_bytes(result: ModelWorkLedgerQueryResult) -> int:
    """The bytes the answer occupies as JSON, which is what the topic carries."""
    return len(result.model_dump_json().encode("utf-8"))


def _answered_count(result: ModelWorkLedgerQueryResult) -> int:
    return (
        len(result.rows)
        + len(result.holds)
        + len(result.open_claims)
        + len(result.latest)
        + sum(len(box.entries) for box in result.inbox)
    )


def _bounded_form(request: ModelWorkLedgerQueryRequest, suggested: int | None) -> str:
    kind = request.query
    if request.history is not None:
        return (
            "ask for a smaller ledger_seq interval of the history range and page "
            "it with the cursor"
        )
    if kind is EnumWorkLedgerQueryKind.LATEST_STATE:
        return (
            "ask for a smaller page_size and follow next_cursor, or narrow with "
            "subject_kind, lanes, tickets, kinds or since"
        )
    if kind in POINT_QUERY_KINDS:
        return (
            "ask for a smaller page_size and follow next_cursor, or add a lane, "
            "ticket, repo, pr or surface filter"
        )
    limit = (
        f"set limit={suggested} (the newest rows are answered), "
        if suggested is not None
        else "set a smaller limit (the newest rows are answered), "
    )
    if kind is EnumWorkLedgerQueryKind.NEWEST_PER_LANE:
        return limit + "or add since, until or kinds to narrow the lanes"
    return limit + "or add lane, ticket, pr, kinds, since or until to narrow the rows"


def bound_answer(
    request: ModelWorkLedgerQueryRequest,
    result: ModelWorkLedgerQueryResult,
    bound: ModelWorkLedgerAnswerBound,
) -> ModelWorkLedgerQueryResult:
    """The answer when it fits under the bound; otherwise its typed refusal."""
    size = answer_size_bytes(result)
    if size <= bound.max_answer_bytes:
        return result
    shown = _answered_count(result)
    suggested = (
        max(1, int(shown * bound.max_answer_bytes / size * _SUGGESTION_FILL))
        if shown
        and request.query not in POINT_QUERY_KINDS
        and request.query is not EnumWorkLedgerQueryKind.LATEST_STATE
        else None
    )
    form = _bounded_form(request, suggested)
    refusal = ModelWorkLedgerRefusal(
        bound_bytes=bound.max_answer_bytes,
        answer_bytes=size,
        matched=result.matched,
        suggested_limit=suggested,
        suggestion=(
            f"the {request.query.value} answer would be {size} bytes against the "
            f"bound of {bound.max_answer_bytes} bytes, so it was not sent; {form}. "
            "Repeating this request refuses again."
        ),
    )
    return ModelWorkLedgerQueryResult(
        correlation_id=result.correlation_id,
        query=result.query,
        window_since=result.window_since,
        window_until=result.window_until,
        matched=result.matched,
        status="refused",
        refusal=refusal,
        page_size=result.page_size,
    )


__all__ = [
    "answer_size_bytes",
    "bound_answer",
    "load_work_ledger_answer_bound",
]
