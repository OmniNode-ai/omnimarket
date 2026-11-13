# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain events of the ledger hold verdict (OMN-19609, cutover plan step L6).

Golden: a hold carried only by the projection answers HELD with the answered event; no hold on a
fresh node answer is CLEAR. Error: an unreadable node with no file lines, a retired fallback, an
unparseable hold line and a stale answer with no hold are UNDECIDED with the refused event, never
CLEAR. Positive control: the same hold read from a file that does not carry it answers CLEAR with
source=file-fallback, which is why CLEAR is trusted only when the node answered. Failing control:
the named break ``_applies`` always false reads the projection-only hold as CLEAR.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.events.hold_verdict import (
    EnumHoldSource,
    EnumHoldVerdict,
    ModelHoldVerdictEvent,
    ModelHoldVerdictOverlay,
    ModelHoldVerdictRequest,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers import (
    handler_hold_verdict,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers.handler_hold_verdict import (
    HandlerHoldVerdict,
    load_topics,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderResult,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 11, 2, 3, 0, 0, tzinfo=UTC)
TOPICS = load_topics()
HOLD_PR = (
    "HOLD hold-a by=orch-1 scope=<to=lane-x;repo=omnimarket;pr=omnimarket#7> "
    "until=2026-11-30T00:00:00Z db:row-hold-a"
)
EMPTY = "HOLD none checked=0 released=0 expired=0 legacy-no-id=0"


class ListSink:
    def __init__(self) -> None:
        self.events: list[ModelHoldVerdictEvent] = []

    def emit(self, event: ModelHoldVerdictEvent) -> None:
        self.events.append(event)


def node_answer(
    lines: tuple[str, ...], status: EnumReaderStatus = EnumReaderStatus.OK
) -> ModelReaderResult:
    answered = status in (EnumReaderStatus.OK, EnumReaderStatus.STALE)
    return ModelReaderResult(
        reader="pause-check",
        query=EnumReaderQuery.HOLDS,
        status=status,
        reason="test",
        source="node" if answered else "none",
        lines=lines if answered else (),
        matched=len(lines) if answered else 0,
        watermark=NOW - timedelta(seconds=20) if answered else None,
        watermark_age_s=20 if answered else None,
        max_watermark_age_s=900,
        parity=EnumReaderParity.UNREAD,
    )


def request(
    answer: ModelReaderResult,
    pr: str = "omnimarket#7",
    lane: str | None = "lane-x",
    file_lines: tuple[str, ...] | None = None,
) -> ModelHoldVerdictRequest:
    return ModelHoldVerdictRequest(
        pr=pr, lane=lane, node_answer=answer, file_fallback_lines=file_lines
    )


def handler(fallback: bool = True) -> tuple[HandlerHoldVerdict, ListSink]:
    sink = ListSink()
    overlay = ModelHoldVerdictOverlay(file_fallback_allowed=fallback, journal_env="J")
    return HandlerHoldVerdict(overlay, sink, clock=lambda: NOW), sink


def test_golden_a_hold_only_in_the_projection_answers_held() -> None:
    h, sink = handler()
    verdict = h.handle(request(node_answer((HOLD_PR,))))
    assert verdict.verdict is EnumHoldVerdict.HELD
    assert verdict.source is EnumHoldSource.NODE
    assert verdict.hold_ids == ("hold-a",)
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.ANSWERED
    assert event.topic == TOPICS["hold_verdict_answered"]
    assert "source=node" in verdict.log_line()


def test_golden_no_hold_on_a_fresh_node_answer_is_clear() -> None:
    h, sink = handler()
    verdict = h.handle(request(node_answer((EMPTY,))))
    assert (verdict.verdict, verdict.source) == (
        EnumHoldVerdict.CLEAR,
        EnumHoldSource.NODE,
    )
    assert sink.events[0].kind is EnumReaderEventKind.ANSWERED


def test_error_chain_unreadable_node_and_no_file_lines_is_undecided() -> None:
    h, sink = handler()
    verdict = h.handle(request(node_answer((), EnumReaderStatus.UNREADABLE)))
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.source is EnumHoldSource.NONE
    assert verdict.reason == "node-unreadable-and-no-file-lines"
    [event] = sink.events
    assert event.kind is EnumReaderEventKind.REFUSED
    assert event.topic == TOPICS["hold_verdict_refused"]


def test_error_chain_retired_fallback_is_undecided_even_with_file_lines() -> None:
    h, sink = handler(fallback=False)
    verdict = h.handle(
        request(node_answer((), EnumReaderStatus.UNREADABLE), file_lines=(EMPTY,))
    )
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.reason == "file-fallback-retired"
    assert sink.events[0].kind is EnumReaderEventKind.REFUSED


def test_error_chain_not_moved_reader_with_no_file_lines_is_undecided() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((), EnumReaderStatus.NOT_MOVED)))
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED


def test_error_chain_unparseable_hold_line_is_undecided_never_clear() -> None:
    h, sink = handler()
    verdict = h.handle(request(node_answer(("HOLD hold-a garbage",))))
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.reason == "unparseable-hold-line"
    assert sink.events[0].kind is EnumReaderEventKind.REFUSED


def test_error_chain_stale_answer_with_no_hold_is_undecided() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((EMPTY,), EnumReaderStatus.STALE)))
    assert verdict.verdict is EnumHoldVerdict.UNDECIDED
    assert verdict.reason == "watermark-stale-and-no-hold"


def test_a_stale_answer_that_carries_an_applying_hold_is_still_held() -> None:
    h, _ = handler()
    verdict = h.handle(request(node_answer((HOLD_PR,), EnumReaderStatus.STALE)))
    assert verdict.verdict is EnumHoldVerdict.HELD


@pytest.mark.parametrize(
    ("scope", "until"),
    [
        ("to=lane-x;repo=omnimarket;pr=omnimarket#7;surface=s", "open"),
        ("to=lane-x;repo=omnimarket;pr=omnimarket#7", "2026-11-01T00:00:00Z"),
        ("repo=omnimarket;pr=omnimarket#8", "open"),
        ("repo=omnibase_infra;pr=omnibase_infra#7", "open"),
        ("repo=omnibase_infra", "open"),
        ("to=lane-y", "open"),
    ],
)
def test_a_hold_that_does_not_apply_leaves_the_pr_clear(scope: str, until: str) -> None:
    line = f"HOLD hold-b by=orch-1 scope=<{scope}> until={until} db:row-b"
    verdict = handler()[0].handle(request(node_answer((line,))))
    assert verdict.verdict is EnumHoldVerdict.CLEAR


@pytest.mark.parametrize(
    "scope",
    [
        "to=all",
        "to=lane-x",
        "repo=OmniNode-ai/omnimarket",
        "repo=omnimarket;pr=OmniNode-ai/omnimarket#7",
        "repo=omnimarket;pr=7",
    ],
)
def test_a_hold_that_applies_holds_the_pr(scope: str) -> None:
    line = f"HOLD hold-c by=orch-1 scope=<{scope}> until=open db:row-c"
    verdict = handler()[0].handle(request(node_answer((line,))))
    assert verdict.verdict is EnumHoldVerdict.HELD
    assert verdict.hold_ids == ("hold-c",)


def test_stale_heading_line_is_not_a_hold() -> None:
    heading = "HOLD stale=1: until=open, no RELEASE, older than 14d; not in force"
    verdict = handler()[0].handle(request(node_answer((heading, EMPTY))))
    assert verdict.verdict is EnumHoldVerdict.CLEAR


def test_a_file_fallback_answer_is_logged_as_such() -> None:
    h, sink = handler()
    file_hold = HOLD_PR.replace("db:row-hold-a", "L123")
    verdict = h.handle(
        request(node_answer((), EnumReaderStatus.UNREADABLE), file_lines=(file_hold,))
    )
    assert verdict.verdict is EnumHoldVerdict.HELD
    assert verdict.source is EnumHoldSource.FILE_FALLBACK
    assert "source=file-fallback" in verdict.log_line()
    assert sink.events[0].source is EnumHoldSource.FILE_FALLBACK


def test_positive_control_a_file_without_the_hold_reads_clear() -> None:
    """The same hold lives only in the projection; a read that went to a file lacking it is CLEAR."""
    verdict = handler()[0].handle(
        request(node_answer((), EnumReaderStatus.UNREADABLE), file_lines=(EMPTY,))
    )
    assert verdict.verdict is EnumHoldVerdict.CLEAR
    assert verdict.source is EnumHoldSource.FILE_FALLBACK
    assert verdict.reason == "file-fallback-answered-no-hold"


def test_break_scope_filter_ignores_holds_reads_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Named break: with ``_applies`` always false the golden HELD above reads CLEAR."""
    answer = node_answer((HOLD_PR,))
    assert handler()[0].handle(request(answer)).verdict is EnumHoldVerdict.HELD
    monkeypatch.setattr(
        handler_hold_verdict, "_applies", lambda *_args, **_kwargs: False
    )
    assert handler()[0].handle(request(answer)).verdict is EnumHoldVerdict.CLEAR
