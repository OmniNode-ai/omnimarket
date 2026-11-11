# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tick unit's pass reads rerun requests off the request topic and answers them (OMN-23830).

Seam: the request topic, the consumer group and the limit are the overlay's rerun_requests section; the request is the
shared model the verb publishes (bare, or in an envelope). Golden: one request on the topic is answered rerun in one
pass, on the completed topic, and a second pass answers the same request duplicate from the ledger file. Error chain:
a record that is not a request is skipped with a typed row and the rest are answered; a request for a job that is not
failed is answered on the failed topic. Control: with no rerun_requests section the same request is never read.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.events.pr_landing_rerun_request.model_landing_rerun_request import (
    ModelLandingRerunRequest,
)
from omnimarket.models.landing_controller_carry import (
    ModelLandingControllerDecisions,
)
from omnimarket.models.landing_tick_record import ModelLandingTickRecord
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.tick_ports import (
    InMemoryTickPublisher,
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

SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "pr_landing_tick_replay"
    / "tick_725_omnimarket.json"
)
RECORD_TOPIC = "onex.evt.omnimarket.pr-landing-tick-facts-recorded.v1"
REQUEST_TOPIC = "onex.cmd.omnimarket.pr-landing-rerun-requested.v1"
COMPLETED = "onex.evt.omnimarket.pr-landing-rerun-completed.v1"
FAILED = "onex.evt.omnimarket.pr-landing-rerun-failed.v1"
PR = "OmniNode-ai/omnimarket#3183"
HEAD = "3a1beb08992004c653336cbb4bf8039d20364b58"
JOB = "occ-autobind / outcome"


def _ask(job: str = JOB) -> ModelLandingRerunRequest:
    return ModelLandingRerunRequest(
        correlation_id=uuid4(),
        pr=PR,
        head_sha=HEAD,
        job=job,
        reason="runner outage",
        requester="lane-a",
    )


class _Readers:
    """One reader per topic, as the factory is asked for them with the topic selection."""

    def __init__(self, record: str, requests: list[str]) -> None:
        self.by_topic = {RECORD_TOPIC: record, REQUEST_TOPIC: requests}
        self.selections: list[ModelTickTopicSelection] = []

    def __call__(self, selection: ModelTickTopicSelection) -> object:
        self.selections.append(selection)
        raw = self.by_topic[selection.topic]
        rows = raw if isinstance(raw, list) else [raw]

        class _Reader:
            def read(self, max_records: int, timeout_s: float) -> Iterable[str]:
                return rows[:max_records]

        return _Reader()


def _record() -> str:
    snap = ModelTickSnapshot.model_validate_json(SNAPSHOT.read_text(encoding="utf-8"))
    return ModelLandingTickRecord(
        correlation_id=uuid4(),
        tick=snap.facts.tick,
        facts=snap.facts,
        provenance="test",
        controller=ModelLandingControllerDecisions(
            controller_tick=1,
            observed_at=snap.facts.observed_at,
            decisions=(),
            source="test",
        ),
    ).model_dump_json()


def _overlay(tmp_path: Path, **update: object) -> ModelTickOrchestratorOverlay:
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
        "host_env": "ONEX_HOST_NAME",
        "join_timeout_s": 5.0,
        "run_limit_s": 60,
        "run_margin_s": 5.0,
        "cutover_repository": "OmniNode-ai/omnimarket",
        "rerun_requests": {
            "max_reruns_per_head": 1,
            "request_topic": REQUEST_TOPIC,
            "consumer_group": "landing-tick-rerun",
            "poll_timeout_s": 1.0,
            "ledger_path": str(tmp_path / "rerun-ledger.jsonl"),
        },
    }
    values.update(update)
    return ModelTickOrchestratorOverlay.model_validate(values)


def _topics(publisher: InMemoryTickPublisher) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for topic, event in publisher.published:
        verdict = getattr(event, "verdict", None)
        if verdict is not None:
            seen.setdefault(topic, []).append(verdict.value)
    return seen


def test_golden_chain_a_request_is_answered_in_the_pass_and_a_later_pass_answers_duplicate(
    tmp_path: Path,
) -> None:
    envelope = json.dumps({"payload": json.loads(_ask().model_dump_json())})
    first = InMemoryTickPublisher()
    readers = _Readers(_record(), [envelope])
    code, line = run_once(
        _overlay(tmp_path), topic_reader_factory=readers, publisher=first
    )
    assert code == 0, line
    assert _topics(first) == {COMPLETED: ["rerun"]}
    assert line["rerun_requests"] == {
        "answered": 1,
        "verdicts": ["rerun"],
        "skipped": [],
    }
    group = {s.topic: s.consumer_group for s in readers.selections}
    assert group[REQUEST_TOPIC] == "landing-tick-rerun-test-host"
    again = InMemoryTickPublisher()
    code, line = run_once(
        _overlay(tmp_path),
        topic_reader_factory=_Readers(_record(), [_ask().model_dump_json()]),
        publisher=again,
    )
    assert code == 0, line
    assert _topics(again) == {COMPLETED: ["duplicate"]}
    assert line["rerun_requests"]["verdicts"] == ["duplicate"]


def test_error_chain_an_unparseable_record_is_skipped_and_a_job_not_failed_is_refused(
    tmp_path: Path,
) -> None:
    publisher = InMemoryTickPublisher()
    code, line = run_once(
        _overlay(tmp_path),
        topic_reader_factory=_Readers(
            _record(),
            ["not json", '{"pr": "x"}', _ask("a green job").model_dump_json()],
        ),
        publisher=publisher,
    )
    assert code == 0, line
    assert _topics(publisher) == {FAILED: ["job_not_failed"]}
    skipped = line["rerun_requests"]["skipped"]
    assert [row["position"] for row in skipped] == [0, 1]
    assert skipped[0]["detail"] == "not_json"


def test_control_without_the_section_no_request_is_read(tmp_path: Path) -> None:
    publisher = InMemoryTickPublisher()
    readers = _Readers(_record(), [_ask().model_dump_json()])
    code, line = run_once(
        _overlay(tmp_path, rerun_requests=None),
        topic_reader_factory=readers,
        publisher=publisher,
    )
    assert code == 0, line
    assert "rerun_requests" not in line
    assert REQUEST_TOPIC not in {s.topic for s in readers.selections}
    assert _topics(publisher) == {}


def test_a_redpanda_section_names_its_topic_and_a_declared_limit(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="request_topic"):
        _overlay(
            tmp_path,
            rerun_requests={"max_reruns_per_head": 1, "consumer_group": "g"},
        )
    with pytest.raises(ValidationError, match="max_reruns_per_head"):
        _overlay(tmp_path, rerun_requests={"request_topic": REQUEST_TOPIC})
