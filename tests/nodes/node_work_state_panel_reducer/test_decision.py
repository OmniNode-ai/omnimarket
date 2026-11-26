# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure decision checks of the work-state panel reducer (OMN-19998).

Every check is a ``problems_<name>()`` list (empty = pass) plus a control test
that injects a NAMED break into the service module (``monkeypatch`` of its
module-level functions) and asserts the same list is then NOT empty: the check
must fail for the one reason the break names, not pass by accident.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError, field_validator

from omnimarket.models.work_state_panel import (
    EnumPanelDecision,
    EnumPanelRefusal,
    ModelWorkStatePanelBody,
    ModelWorkStatePanelDecision,
    ModelWorkStatePanelEntry,
    ModelWorkStatePanelRow,
)
from omnimarket.models.work_state_projection.enum_work_item_state import (
    EnumWorkItemState,
)
from omnimarket.models.work_state_projection.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.models.work_state_projection.model_work_state_projected_event import (
    EnumWorkStateProjectorProfile,
    ModelWorkStateProjectedEvent,
)
from omnimarket.nodes.node_work_state_panel_reducer.handlers import (
    service_work_state_panel_reducer as svc,
)
from tests.nodes.node_work_state_panel_reducer.support import (
    MARKER,
    NOW,
    T0,
    entry,
    event,
)

pytestmark = pytest.mark.unit

REFUSAL_ID_SHAPE = re.compile(r"refused-[0-9a-f]{64}")


def _decide(
    held: ModelWorkStatePanelRow | None,
    incoming: ModelWorkStateProjectedEvent,
    now: datetime = NOW,
    panel: ModelWorkStatePanelEntry | None = None,
) -> tuple[ModelWorkStatePanelDecision | None, str | None]:
    """One decision; an exception a break makes the service raise is a problem, not an error."""
    try:
        decision = svc.WorkStatePanelRowCommitter().decide_publication(
            held,
            incoming,
            now,
            panel if panel is not None else entry(),
        )
    except Exception as exc:  # a control's break must surface as a problem, not abort
        return None, f"decide_publication raised {type(exc).__name__}: {exc}"
    return decision, None


def _final_held_body(
    *arrivals: ModelWorkStateProjectedEvent,
) -> ModelWorkStatePanelBody | None:
    """The held body after folding the arrivals in the given order (None on any surprise)."""
    held: ModelWorkStatePanelRow | None = None
    for incoming in arrivals:
        decision, problem = _decide(held, incoming)
        if problem is not None or decision is None or decision.row is None:
            return None
        held = decision.row
    return held.body


def _naive_event(**override: Any) -> ModelWorkStateProjectedEvent:
    """A wire event built with ``model_construct`` so a naive datetime survives construction."""
    return ModelWorkStateProjectedEvent.model_construct(
        **{**event(100).model_dump(), **override}
    )


# --- 1. the fold order: timestamps order, the digest breaks as_of ties --------------------


def _break_timestamp_only_order_key(
    body: ModelWorkStatePanelBody,
) -> tuple[datetime]:
    """BREAK timestamp_only_last_arrival_wins: order by timestamp alone, no digest tie-break."""
    return (body.as_of,)


def _break_last_arrival_wins(
    candidate_key: tuple[datetime],
    held_key: tuple[datetime],
) -> bool:
    """BREAK timestamp_only_last_arrival_wins: >= makes an equal timestamp count as newer."""
    return candidate_key >= held_key


def problems_decision_order() -> list[str]:
    problems: list[str] = []
    decisions: list[ModelWorkStatePanelDecision] = []
    held: ModelWorkStatePanelRow | None = None
    for as_of_s in (100, 200, 100, 200):
        decision, problem = _decide(held, event(as_of_s))
        if problem is not None or decision is None:
            return [problem or "no decision"]
        decisions.append(decision)
        held = decision.row
    expected_outcomes = (
        EnumPanelDecision.ACCEPTED,
        EnumPanelDecision.ACCEPTED,
        EnumPanelDecision.SUPERSEDED,
        EnumPanelDecision.DUPLICATE,
    )
    for index, (decision, expected) in enumerate(
        zip(decisions, expected_outcomes, strict=True)
    ):
        if decision.outcome is not expected:
            problems.append(
                f"event {index}: outcome {decision.outcome.value} != {expected.value}"
            )
    for index, (decision, expected_epoch) in enumerate(
        zip(decisions, (1, 2, 2, 2), strict=True)
    ):
        if decision.row is None or decision.row.epoch != expected_epoch:
            problems.append(f"event {index}: epoch != {expected_epoch}")
    if held is None or held.as_of != T0 + timedelta(seconds=200):
        problems.append("the held row's as_of is not the 200 s event's instant")
    if not (
        decisions[2].row is decisions[1].row and decisions[3].row is decisions[1].row
    ):
        problems.append("superseded/duplicate did not return the held row itself")
    # same as_of, different bodies: the winner must not depend on arrival order
    if _final_held_body(event(100), event(100, item_count=4)) != _final_held_body(
        event(100, item_count=4),
        event(100),
    ):
        problems.append("a same-as_of tie is resolved by arrival order, not by content")
    return problems


def test_decision_order() -> None:
    assert problems_decision_order() == []


def test_control_decision_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "order_key", _break_timestamp_only_order_key)
    monkeypatch.setattr(svc, "is_newer", _break_last_arrival_wins)
    assert problems_decision_order() != []


# --- 2. key mismatch and undecodable bodies -----------------------------------------------


def _break_unvalidated_body_from_event(
    incoming: ModelWorkStateProjectedEvent,
) -> ModelWorkStatePanelBody:
    """BREAK unvalidated_body_from_event: model_construct, so a bogus counts key slips through."""
    return ModelWorkStatePanelBody.model_construct(
        as_of=incoming.as_of,
        computed_at=incoming.computed_at,
        projector=incoming.projector,
        item_count=incoming.item_count,
        counts=dict(incoming.counts),
        ready_in_scope=incoming.ready_in_scope,
        unread=tuple(incoming.unread),
        stale_sources=tuple(incoming.stale_sources),
    )


def problems_key_mismatch_and_body() -> list[str]:
    problems: list[str] = []
    decision, problem = _decide(
        None,
        event(100, projector=EnumWorkStateProjectorProfile.UNREAD),
    )
    if problem is not None:
        problems.append(problem)
    elif (
        decision is None
        or decision.outcome is not EnumPanelDecision.KEY_MISMATCH
        or decision.row is not None
    ):
        problems.append("a projector mismatch must be KEY_MISMATCH with no row")

    decision, problem = _decide(None, event(100, counts={"bogus": 1}))
    if problem is not None:
        problems.append(problem)
    elif decision is None or decision.outcome is not EnumPanelDecision.UNDECODABLE:
        problems.append("a bogus counts key must be UNDECODABLE")
    row = decision.row if decision is not None else None
    if row is None:
        problems.append("an undecodable event with nothing held wrote no row")
    else:
        if row.epoch != 0:
            problems.append("a refusal row with nothing held must be at epoch 0")
        if row.publication_id is not None:
            problems.append("a refusal row must not hold a publication id")
        if row.last_refusal_code is not EnumPanelRefusal.UNDECODABLE:
            problems.append("the refusal row's code is not undecodable")

    accepted, problem = _decide(None, event(100))
    if problem is not None or accepted is None or accepted.row is None:
        problems.append(problem or "the held fixture was not accepted")
    else:
        held = accepted.row
        refusal, problem = _decide(held, event(100, counts={"bogus": 1}))
        if problem is not None or refusal is None or refusal.row is None:
            problems.append(problem or "the refusal against a held row wrote no row")
        else:
            after = refusal.row
            for unchanged in (
                "epoch",
                "as_of",
                "publication_id",
                "body",
                "received_at",
            ):
                if getattr(after, unchanged) != getattr(held, unchanged):
                    problems.append(f"a refusal changed {unchanged}")
            for changed in ("last_refusal_code", "last_refusal_at", "last_refusal_id"):
                if getattr(after, changed) == getattr(held, changed):
                    problems.append(f"a refusal left {changed} unchanged")

    decision, problem = _decide(None, event(100, unread=("nosuchsource:x",)))
    if problem is not None:
        problems.append(problem)
    elif decision is None or decision.outcome is not EnumPanelDecision.UNDECODABLE:
        problems.append("an unknown source name must be UNDECODABLE")
    return problems


def test_key_mismatch_and_body() -> None:
    assert problems_key_mismatch_and_body() == []


def test_control_key_mismatch_and_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "body_from_event", _break_unvalidated_body_from_event)
    assert problems_key_mismatch_and_body() != []


# --- 3. refusal ids are opaque digests -----------------------------------------------------


def _break_marker_text_refusal_id(incoming: ModelWorkStateProjectedEvent) -> str:
    """BREAK marker_text_refusal_id: an id carrying the event's text instead of a digest."""
    return "refused-" + MARKER


def problems_refusal_ids_opaque() -> list[str]:
    problems: list[str] = []
    refusals = (
        (event(100, counts={MARKER: 1}), EnumPanelDecision.UNDECODABLE),
        (event(100, unread=(MARKER + ":x",)), EnumPanelDecision.UNDECODABLE),
        # NOW + 61 s against the entry's 60 s future skew
        (event(361, unread=("ledger:" + MARKER,)), EnumPanelDecision.FUTURE_DATED),
    )
    for index, (incoming, expected) in enumerate(refusals):
        decision, problem = _decide(None, incoming)
        if problem is not None or decision is None:
            problems.append(problem or f"refusal {index}: no decision")
            continue
        if decision.outcome is not expected:
            problems.append(
                f"refusal {index}: outcome {decision.outcome.value} != {expected.value}"
            )
        row = decision.row
        if row is None:
            problems.append(f"refusal {index}: no row")
            continue
        if (
            row.last_refusal_id is None
            or REFUSAL_ID_SHAPE.fullmatch(row.last_refusal_id) is None
        ):
            problems.append(f"refusal {index}: the id is not a bare sha256 digest")
        if MARKER in row.model_dump_json():
            problems.append(f"refusal {index}: the row carries the event's text")
    # the same event twice -> the same id; a different event -> a different id
    again, _ = _decide(None, refusals[1][0])
    repeat, _ = _decide(None, refusals[1][0])
    other, _ = _decide(None, event(100, unread=(MARKER + ":y",)))
    if again is None or repeat is None or again.row is None or repeat.row is None:
        problems.append("the same unread refusal did not decide twice")
    elif again.row.last_refusal_id != repeat.row.last_refusal_id:
        problems.append("the same event refused twice got different ids")
    if other is None or other.row is None:
        problems.append("the variant event did not refuse")
    elif (
        again is not None
        and again.row is not None
        and (other.row.last_refusal_id == again.row.last_refusal_id)
    ):
        problems.append("different events got the same id")
    return problems


def test_refusal_ids_opaque(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        problems = problems_refusal_ids_opaque()
    assert problems == []
    assert MARKER not in caplog.text


def test_control_refusal_ids_opaque(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "refusal_id", _break_marker_text_refusal_id)
    assert problems_refusal_ids_opaque() != []
    with pytest.raises(ValidationError):
        svc.WorkStatePanelRowCommitter().decide_publication(
            None,
            event(100, counts={MARKER: 1}),
            NOW,
            entry(),
        )


# --- 4. the cause text after a source's ':' is dropped --------------------------------------


def _break_unsplit_source_names_body_from_event(
    incoming: ModelWorkStateProjectedEvent,
) -> ModelWorkStatePanelBody:
    """BREAK unsplit_source_names: EnumWorkStateSource on the whole 'source:cause' text."""

    def whole(names: tuple[str, ...]) -> tuple[EnumWorkStateSource, ...]:
        return tuple(EnumWorkStateSource(name) for name in names)

    return ModelWorkStatePanelBody(
        as_of=incoming.as_of,
        computed_at=incoming.computed_at,
        projector=incoming.projector,
        item_count=incoming.item_count,
        counts={
            EnumWorkItemState(key): value for key, value in incoming.counts.items()
        },
        ready_in_scope=incoming.ready_in_scope,
        unread=whole(incoming.unread),
        stale_sources=whole(incoming.stale_sources),
    )


def problems_cause_text_dropped() -> list[str]:
    problems: list[str] = []
    incoming = event(
        100,
        unread=("ledger:" + MARKER, "linear:note"),
        stale_sources=("plans:" + MARKER,),
    )
    decision, problem = _decide(None, incoming)
    if problem is not None or decision is None:
        return [problem or "no decision"]
    if decision.outcome is not EnumPanelDecision.ACCEPTED:
        problems.append(f"outcome {decision.outcome.value} != accepted")
    row = decision.row
    if row is None or row.body is None:
        problems.append("the accepted event left no held body")
        return problems
    if row.body.unread != (EnumWorkStateSource.LEDGER, EnumWorkStateSource.LINEAR):
        problems.append("unread did not collapse to its bare sources")
    if row.body.stale_sources != (EnumWorkStateSource.PLANS,):
        problems.append("stale_sources did not collapse to its bare sources")
    if MARKER in row.model_dump_json():
        problems.append("the row carries the cause text")
    return problems


def test_cause_text_dropped() -> None:
    assert problems_cause_text_dropped() == []


def test_control_cause_text_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        svc,
        "body_from_event",
        _break_unsplit_source_names_body_from_event,
    )
    assert problems_cause_text_dropped() != []


# --- 5. future-dated events -----------------------------------------------------------------


def problems_future_dated(first_skew_s: int = 60) -> list[str]:
    problems: list[str] = []
    decision, problem = _decide(
        None, event(361), NOW, entry(max_future_skew_s=first_skew_s)
    )
    if problem is not None or decision is None:
        return [problem or "no decision"]
    if decision.outcome is not EnumPanelDecision.FUTURE_DATED:
        problems.append(
            f"at NOW+61 s: outcome {decision.outcome.value} != future_dated"
        )
    if decision.row is not None and decision.row.publication_id is not None:
        problems.append("a future-dated row holds a publication")
    held = decision.row

    decision, problem = _decide(held, event(200))
    if (
        problem is not None
        or decision is None
        or (decision.outcome is not EnumPanelDecision.ACCEPTED)
    ):
        problems.append(problem or "a valid event after a refusal was not accepted")
    held = decision.row if decision is not None and decision.row is not None else held

    decision, problem = _decide(held, event(360))  # exactly NOW+60 s: the budget's edge
    if (
        problem is not None
        or decision is None
        or (decision.outcome is not EnumPanelDecision.ACCEPTED)
    ):
        problems.append(problem or "an event at the skew edge was not accepted")
    held = decision.row if decision is not None and decision.row is not None else held

    decision, problem = _decide(held, event(200), NOW + timedelta(hours=1))
    if (
        problem is not None
        or decision is None
        or (decision.outcome is not EnumPanelDecision.SUPERSEDED)
    ):
        problems.append(problem or "an older event an hour later was not superseded")
    return problems


def test_future_dated() -> None:
    assert problems_future_dated() == []


def test_control_future_dated() -> None:
    # BREAK unlimited_future_skew: the entry's budget swallows the 61 s jump, so it is accepted
    assert problems_future_dated(first_skew_s=100_000) != []


# --- 6. naive timestamps are undecodable ----------------------------------------------------


def _break_tzinfo_forgiving_body_from_event(
    incoming: ModelWorkStateProjectedEvent,
) -> ModelWorkStatePanelBody:
    """BREAK tzinfo_forgiving_body_from_event: stamps UTC on naive datetimes instead of refusing."""

    def bare(names: tuple[str, ...]) -> tuple[EnumWorkStateSource, ...]:
        return tuple(EnumWorkStateSource(name.split(":", 1)[0]) for name in names)

    return ModelWorkStatePanelBody(
        as_of=incoming.as_of.replace(tzinfo=UTC),
        computed_at=incoming.computed_at.replace(tzinfo=UTC),
        projector=incoming.projector,
        item_count=incoming.item_count,
        counts={
            EnumWorkItemState(key): value for key, value in incoming.counts.items()
        },
        ready_in_scope=incoming.ready_in_scope,
        unread=bare(incoming.unread),
        stale_sources=bare(incoming.stale_sources),
    )


def problems_naive_timestamp_undecodable() -> list[str]:
    problems: list[str] = []
    naive = datetime(2026, 11, 14, 12, 0)  # T0 without its tzinfo
    for field in ("as_of", "computed_at"):
        decision, problem = _decide(None, _naive_event(**{field: naive}))
        if problem is not None or decision is None:
            problems.append(problem or f"naive {field}: no decision")
            continue
        if decision.outcome is not EnumPanelDecision.UNDECODABLE:
            problems.append(
                f"naive {field}: outcome {decision.outcome.value} != undecodable"
            )
        if decision.row is not None and decision.row.publication_id is not None:
            problems.append(f"naive {field}: the row holds a publication")
    return problems


def test_naive_timestamp_undecodable() -> None:
    assert problems_naive_timestamp_undecodable() == []


def test_control_naive_timestamp_undecodable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "body_from_event", _break_tzinfo_forgiving_body_from_event)
    assert problems_naive_timestamp_undecodable() != []


# --- 7. one instant, one identity, whatever the source offset -------------------------------


def _break_offset_preserving_body_from_event(
    incoming: ModelWorkStateProjectedEvent,
) -> ModelWorkStatePanelBody:
    """BREAK raw_string_publication_id: model_construct keeps the source offset un-normalized."""

    def bare(names: tuple[str, ...]) -> tuple[EnumWorkStateSource, ...]:
        return tuple(EnumWorkStateSource(name.split(":", 1)[0]) for name in names)

    return ModelWorkStatePanelBody.model_construct(
        as_of=incoming.as_of,
        computed_at=incoming.computed_at,
        projector=incoming.projector,
        item_count=incoming.item_count,
        counts={
            EnumWorkItemState(key): value for key, value in incoming.counts.items()
        },
        ready_in_scope=incoming.ready_in_scope,
        unread=bare(incoming.unread),
        stale_sources=bare(incoming.stale_sources),
    )


def _break_raw_string_publication_id(body: ModelWorkStatePanelBody) -> str:
    """BREAK raw_string_publication_id: the id from the body's as_of string, offsets and all."""
    as_of = body.as_of.isoformat(timespec="microseconds")
    return f"{body.projector.value}|{as_of}|{body.digest()}"


def problems_utc_canonical() -> list[str]:
    problems: list[str] = []
    utc = datetime(2026, 11, 14, 12, 5, tzinfo=UTC)
    shifted = (T0 + timedelta(minutes=5)).astimezone(
        timezone(timedelta(hours=5, minutes=30))
    )
    if utc != shifted:
        return ["fixture: the two datetimes are not the same instant"]
    try:
        utc_body = svc.body_from_event(event(0, as_of=utc))
        shifted_body = svc.body_from_event(event(0, as_of=shifted))
        if svc.publication_id(utc_body) != svc.publication_id(shifted_body):
            problems.append("the publication id depends on the source offset")
        if svc.order_key(utc_body) != svc.order_key(shifted_body):
            problems.append("the order key depends on the source offset")
        for label, body in (("utc", utc_body), ("shifted", shifted_body)):
            if body.as_of.utcoffset() != timedelta(0):
                problems.append(f"the {label} body's as_of is not stored at UTC")
    except Exception as exc:  # a control's break must surface as a problem, not abort
        problems.append(f"canonicalization raised {type(exc).__name__}: {exc}")
    return problems


def test_utc_canonical() -> None:
    assert problems_utc_canonical() == []


def test_control_utc_canonical(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        svc,
        "body_from_event",
        _break_offset_preserving_body_from_event,
    )
    monkeypatch.setattr(svc, "publication_id", _break_raw_string_publication_id)
    assert problems_utc_canonical() != []


# --- 8. an empty counts map is a legal body -------------------------------------------------


class _BreakNoEmptyCountsBody(ModelWorkStatePanelBody):
    """BREAK no_empty_counts_body: a body that refuses an empty counts map."""

    @field_validator("counts")
    @classmethod
    def _counts_not_empty(
        cls,
        counts: dict[EnumWorkItemState, int],
    ) -> dict[EnumWorkItemState, int]:
        if not counts:
            raise ValueError("counts must not be empty")
        return counts


def problems_empty_counts_fixture() -> list[str]:
    problems: list[str] = []
    # a SYNTHETIC fixture, not a producer output: the projection always emits at least one state
    decision, problem = _decide(None, event(100, counts={}, item_count=0))
    if problem is not None or decision is None:
        problems.append(problem or "no decision")
    elif decision.outcome is not EnumPanelDecision.ACCEPTED:
        problems.append(f"outcome {decision.outcome.value} != accepted")
    return problems


def test_empty_counts_fixture() -> None:
    assert problems_empty_counts_fixture() == []


def test_control_empty_counts_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(svc, "ModelWorkStatePanelBody", _BreakNoEmptyCountsBody)
    assert problems_empty_counts_fixture() != []
