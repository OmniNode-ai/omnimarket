# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick triggers the liveness read for each compared pull request head (OMN-26024).

node_pr_landing_liveness_read_effect had no caller. The tick orchestrator now asks it for the heads it compares,
after the comparison is final, and keeps one read per head on its result; the runtime prints one liveness line per
read. Golden: two compared heads are read once each and their events go out on the liveness topics. Error: a
reader that raises, an unreadable head and an overlay that does not load each end as an unreadable read and leave
the tick compared. Control: with reads on or off the comparison, the published compared event and the writes are
the same, so a read changes no landing decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from pydantic import BaseModel

from omnimarket.models.landing_controller_carry import decisions_of
from omnimarket.models.landing_decision import EnumLivenessRead
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.handler_pr_landing_liveness_read import (
    HandlerPrLandingLivenessRead,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.handlers.liveness_read_ports import (
    InMemoryLivenessReader,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    ModelLivenessReadOverlay,
)
from omnimarket.nodes.node_pr_landing_liveness_read_effect.overlay import (
    load_overlay as load_liveness_overlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_liveness import (
    NodeLivenessTickReader,
    UnavailableLivenessReader,
    build_liveness_reader,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    InMemoryTickWriter,
    ProtocolTickPublisher,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickLivenessRead,
    ModelTickRequest,
    ModelTickResult,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick import (
    main,
    run_once,
)
from tests.nodes.node_pr_landing_snapshot_effect.controller_inputs import (
    REPO,
    carry,
    node_facts_bare,
)

pytestmark = pytest.mark.unit

LIVENESS_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_pr_landing_liveness_read_effect/overlay.example.yaml"
)
COMPLETED = "onex.evt.omnimarket.pr-landing-liveness-read-completed.v1"
FAILED = "onex.evt.omnimarket.pr-landing-liveness-read-failed.v1"
HEAD_A, HEAD_B = "a" * 40, "b" * 40
PR_A, PR_B = f"{REPO}#713", f"{REPO}#714"
TIMER = "deploy/systemd/onex-new.timer"


class _CountingReader:
    """A reader that records each (pr, head) it was asked about."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, str]] = []

    def read(self, pr: str, head_sha: str) -> ModelTickLivenessRead:
        self.asked.append((pr, head_sha))
        return ModelTickLivenessRead(
            pr=pr, head_sha=head_sha, read=EnumLivenessRead.READ
        )


class _RaisingReader:
    def read(self, pr: str, head_sha: str) -> ModelTickLivenessRead:
        raise RuntimeError("reader down")


def _request() -> ModelTickRequest:
    return ModelTickRequest(
        correlation_id=uuid4(),
        mode=EnumTickMode.OBSERVE,
        facts=node_facts_bare(),
        controller=decisions_of(carry()),
    )


def _handler(
    publisher: InMemoryTickPublisher,
    writer: InMemoryTickWriter | None = None,
    **kwargs: Any,
) -> HandlerPrLandingTickOrchestrator:
    return HandlerPrLandingTickOrchestrator(
        publisher=publisher, writer=writer, cutover_repository=REPO, **kwargs
    )


def _liveness_overlay() -> ModelLivenessReadOverlay:
    doc = yaml.safe_load(LIVENESS_EXAMPLE.read_text())
    return load_liveness_overlay(explicit=doc)


def _node_reader(
    publisher: ProtocolTickPublisher,
    found: dict[tuple[str, str], tuple[str, ...]],
    unreadable: dict[tuple[str, str], str] | None = None,
) -> NodeLivenessTickReader:
    overlay = _liveness_overlay()
    reader = InMemoryLivenessReader(found, unreadable)
    return NodeLivenessTickReader(
        lambda best_effort: HandlerPrLandingLivenessRead(overlay, reader, best_effort),
        publisher,
    )


def test_golden_chain_each_compared_head_is_read_once_and_published() -> None:
    publisher = InMemoryTickPublisher()
    reader = _node_reader(
        publisher,
        {(REPO, HEAD_A): (TIMER,), (REPO, HEAD_B): ()},
    )
    result = _handler(publisher, liveness_reader=reader, max_liveness_reads=10).handle(
        _request()
    )
    assert result.status is EnumTickStatus.COMPARED
    assert [(r.pr, r.head_sha, r.read) for r in result.liveness_reads] == [
        (PR_A, HEAD_A, EnumLivenessRead.READ),
        (PR_B, HEAD_B, EnumLivenessRead.READ),
    ]
    assert result.liveness_reads[0].undeclared_sources == (TIMER,)
    assert {r.topic for r in result.liveness_reads} == {COMPLETED}
    topics = [topic for topic, _ in publisher.published]
    assert topics.count(COMPLETED) == 2


def test_golden_chain_the_read_is_called_for_the_compared_heads_and_no_other() -> None:
    reader = _CountingReader()
    _handler(
        InMemoryTickPublisher(), liveness_reader=reader, max_liveness_reads=10
    ).handle(_request())
    assert reader.asked == [(PR_A, HEAD_A), (PR_B, HEAD_B)]


def test_golden_chain_without_a_reader_the_tick_makes_no_read() -> None:
    result = _handler(InMemoryTickPublisher()).handle(_request())
    assert result.liveness_reads == ()


def test_golden_chain_max_reads_bounds_the_reads_of_one_pass() -> None:
    reader = _CountingReader()
    _handler(
        InMemoryTickPublisher(), liveness_reader=reader, max_liveness_reads=1
    ).handle(_request())
    assert reader.asked == [(PR_A, HEAD_A)]


def test_control_a_read_changes_no_decision_comparison_event_or_write() -> None:
    plain_pub, read_pub = InMemoryTickPublisher(), InMemoryTickPublisher()
    plain_writer, read_writer = InMemoryTickWriter(), InMemoryTickWriter()
    request = _request()
    plain = _handler(plain_pub, plain_writer).handle(request)
    read = _handler(
        read_pub,
        read_writer,
        liveness_reader=_node_reader(read_pub, {(REPO, HEAD_A): (TIMER,)}),
        max_liveness_reads=10,
    ).handle(request)
    assert read.liveness_reads
    assert read.model_dump(exclude={"liveness_reads"}) == plain.model_dump(
        exclude={"liveness_reads"}
    )
    plain_events = [(t, e) for t, e in plain_pub.published]
    read_events = [
        (t, e) for t, e in read_pub.published if t not in (COMPLETED, FAILED)
    ]
    assert read_events == plain_events
    assert read_writer.performed == plain_writer.performed == []


def test_error_chain_an_unreadable_head_publishes_the_failed_event_and_the_tick_stays_compared() -> (
    None
):
    publisher = InMemoryTickPublisher()
    reader = _node_reader(
        publisher,
        {(REPO, HEAD_B): ()},
        unreadable={(REPO, HEAD_A): "the checkout is not at the head"},
    )
    result = _handler(publisher, liveness_reader=reader, max_liveness_reads=10).handle(
        _request()
    )
    assert result.status is EnumTickStatus.COMPARED
    first, second = result.liveness_reads
    assert first.read is EnumLivenessRead.UNREADABLE
    assert first.detail == "the checkout is not at the head"
    assert first.topic == FAILED
    assert second.read is EnumLivenessRead.READ


def test_error_chain_a_reader_that_raises_is_an_unreadable_read_not_a_failed_tick() -> (
    None
):
    result = _handler(
        InMemoryTickPublisher(),
        liveness_reader=_RaisingReader(),
        max_liveness_reads=10,
    ).handle(_request())
    assert result.status is EnumTickStatus.COMPARED
    assert {r.read for r in result.liveness_reads} == {EnumLivenessRead.UNREADABLE}
    assert result.liveness_reads[0].detail == "the liveness read raised RuntimeError"


def test_error_chain_a_publisher_that_raises_keeps_the_fact_and_names_the_error() -> (
    None
):
    class _Down:
        def publish(self, topic: str, event: BaseModel) -> None:
            raise ConnectionError("broker down")

    reader = _node_reader(_Down(), {(REPO, HEAD_A): ()})
    read = reader.read(PR_A, HEAD_A)
    assert read.read is EnumLivenessRead.READ
    assert read.publish_error == "ConnectionError"


def test_error_chain_an_overlay_that_does_not_load_names_why_each_read_was_not_made(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    reader = build_liveness_reader(InMemoryTickPublisher())
    assert isinstance(reader, UnavailableLivenessReader)
    read = reader.read(PR_A, HEAD_A)
    assert read.read is EnumLivenessRead.UNREADABLE
    assert "the liveness read overlay did not load" in read.detail


def _tick_overlay(**update: object) -> ModelTickOrchestratorOverlay:
    values: dict[str, object] = {
        "backend": "inmemory",
        "act_enabled": False,
        "max_snapshots": 10,
        "floor_piece": 9,
        "cutover_repository": REPO,
    }
    values.update(update)
    return ModelTickOrchestratorOverlay.model_validate(values)


def test_overlay_error_a_zero_read_bound_is_refused() -> None:
    with pytest.raises(ValueError, match="max_reads_per_pass"):
        _tick_overlay(liveness_reads={"max_reads_per_pass": 0})


def test_runtime_pass_prints_one_liveness_line_per_read_and_keeps_the_status_line(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from tests.nodes.node_pr_landing_tick_orchestrator import test_runtime_tick as rt

    snap, repository = rt._cutover()
    reader = _CountingReader()
    overlay = rt._overlay(
        tmp_path, repository, liveness_reads={"max_reads_per_pass": 5}
    )
    code, line = run_once(
        overlay,
        topic_reader_factory=lambda _selection: rt._Reader([rt._record(snap)]),
        publisher=InMemoryTickPublisher(),
        liveness_reader=reader,
    )
    assert code == 0, line
    assert line["status"] == "compared"
    reads = line["liveness_reads"]
    assert isinstance(reads, list)
    assert reads
    assert len(reader.asked) == len(reads)

    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick.load_tick_overlay",
        lambda: overlay,
    )
    monkeypatch.setattr(
        "omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick.run_once",
        lambda _overlay: (code, {**line}),
    )
    assert main([]) == 0
    printed = [json.loads(row) for row in capsys.readouterr().out.splitlines()]
    assert [list(row) for row in printed[:-1]] == [["liveness_read"]] * len(reads)
    assert printed[-1]["status"] == "compared"
    assert printed[-1]["liveness_read_count"] == len(reads)
    assert "liveness_reads" not in printed[-1]


def test_runtime_pass_with_no_liveness_section_makes_no_read(tmp_path: Path) -> None:
    from tests.nodes.node_pr_landing_tick_orchestrator import test_runtime_tick as rt

    snap, repository = rt._cutover()
    reader = _CountingReader()
    code, line = run_once(
        rt._overlay(tmp_path, repository),
        topic_reader_factory=lambda _selection: rt._Reader([rt._record(snap)]),
        publisher=InMemoryTickPublisher(),
        liveness_reader=reader,
    )
    assert code == 0, line
    assert reader.asked == []
    assert "liveness_reads" not in line


def test_result_model_default_carries_no_reads() -> None:
    assert ModelTickResult.model_fields["liveness_reads"].default == ()
