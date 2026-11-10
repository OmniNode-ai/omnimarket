# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick orchestrator's runtime entry: one observe pass over the snapshot step's records (OMN-23514).

The records come through the reader factory the overlay selects (a fake reader here; the bus reader in the
unit). Golden: a cutover record compares, writes nothing and leaves a start and a success in the journal.
Error: a record that does not validate ends the pass unread. Control: act mode in the overlay is refused
before any record is read, and without the cutover setting every record fails with no recorded decision.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
    NodePathDecider,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    ModelTickSnapshot,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_overlay import (
    ModelTickOrchestratorOverlay,
    ModelTickTopicSelection,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.runtime_tick import run_once

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "scenario_s22_green_disarmed_hold_release.json"
)
COMPARED_TOPIC = "onex.evt.omnimarket.pr-landing-tick-compared.v1"
RECORD_TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"


class _Reader:
    def __init__(self, raw: list[str]) -> None:
        self._raw = raw
        self.selection: ModelTickTopicSelection | None = None

    def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
        return self._raw[:max_records]


def _cutover() -> tuple[ModelTickSnapshot, str]:
    snap = ModelTickSnapshot.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    decision = NodePathDecider().decide(snap.facts, snap.fair_share)
    repos = [a.subject.partition("#")[0] for a in decision.actions if "#" in a.subject]
    assert repos, "the fixture tick holds no pull request action"
    return snap, repos[0]


def _record(snap: ModelTickSnapshot) -> str:
    return ModelLandingTickRecord(
        correlation_id=uuid4(),
        tick=snap.facts.tick,
        facts=snap.facts,
        provenance="test",
    ).model_dump_json()


def _overlay(
    tmp_path: Path, repository: str | None, **update: object
) -> ModelTickOrchestratorOverlay:
    values: dict[str, object] = {
        "backend": "redpanda",
        "act_enabled": False,
        "max_snapshots": 10,
        "floor_piece": 9,
        "emission_journal": str(tmp_path / "emissions.jsonl"),
        "bus_lane": "dev",
        "workspace_root": str(tmp_path),
        "consumer_group": "landing-tick-cutover",
        "snapshot_topic": RECORD_TOPIC,
        "poll_timeout_s": 1.0,
        "cutover_repository": repository,
        "script_out_of_scope_repositories": [repository] if repository else [],
    }
    values.update(update)
    return ModelTickOrchestratorOverlay.model_validate(values)


def _journal(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "emissions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_golden_chain_observe_pass_compares_the_cutover_record(tmp_path: Path) -> None:
    snap, repository = _cutover()
    reader = _Reader([_record(snap)])
    publisher = InMemoryTickPublisher()
    code, line = run_once(
        _overlay(tmp_path, repository),
        topic_reader_factory=lambda _selection: reader,
        publisher=publisher,
    )
    assert code == 0, line
    assert line["status"] == "compared"
    assert line["mode"] == "observe"
    assert line["records"] == 1
    assert line["compared"] == 1
    assert line["ticks"] == [snap.facts.tick]
    assert [topic for topic, _ in publisher.published] == [COMPARED_TOPIC]
    phases = [str(event["phase"]) for event in _journal(tmp_path)]
    assert phases[0].endswith("start")
    assert phases[-1].endswith("success")


def test_error_chain_a_record_that_does_not_validate_is_unread(tmp_path: Path) -> None:
    _, repository = _cutover()
    reader = _Reader(['{"tick": "not a record"}'])
    code, line = run_once(
        _overlay(tmp_path, repository), topic_reader_factory=lambda _selection: reader
    )
    assert code == 3
    assert line["status"] == "source_unread"
    assert line["detail"] == "ValidationError"


def test_control_act_mode_is_refused_before_any_read(tmp_path: Path) -> None:
    snap, repository = _cutover()
    reads: list[ModelTickTopicSelection] = []

    def factory(selection: ModelTickTopicSelection) -> _Reader:
        reads.append(selection)
        return _Reader([_record(snap)])

    overlay = _overlay(tmp_path, repository, act_enabled=True, github_mode="dry_run")
    code, line = run_once(overlay, topic_reader_factory=factory)
    assert code == 3
    assert line["status"] == "act_refused"
    assert reads == []


def test_control_without_the_cutover_setting_every_record_fails(tmp_path: Path) -> None:
    snap, _ = _cutover()
    reader = _Reader([_record(snap)])
    code, line = run_once(
        _overlay(tmp_path, None), topic_reader_factory=lambda _selection: reader
    )
    assert code == 1
    assert line["status"] == "failed"
    assert line["failed"] == 1
    phases = [str(event["phase"]) for event in _journal(tmp_path)]
    assert phases[-1].endswith("failure")
