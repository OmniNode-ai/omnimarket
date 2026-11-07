# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam deadline reading demands on-demand-only seams and carries seam counts (OMN-22909).

Golden: an automatic seam with a bound sender and an on-demand seam attached for the run both read
WORKING, and the row carries the seam counts. Error chain: a required automatic seam with no bound
sender makes the run NOT_WORKING and is named; an on-demand seam with nothing to attach it reads
NOT_WORKING; an on-demand node that is slow reads NOT_WORKING. A reading with no declarations keeps
the row it had.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.adapters.inmemory_seam_bus import (
    InMemorySeamBus,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.handlers.handler_seam_deadline import (
    HandlerSeamDeadline,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_seam_deadline_probe_effect.overlay import (
    ModelSeamDeadlineOverlay,
)
from omnimarket.nodes.node_seam_read_orchestrator.adapters.inmemory_node_attacher import (
    InMemoryNodeAttacher,
)

pytestmark = pytest.mark.unit

_EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_deadline_probe_effect/overlay.example.yaml"
)
_NOW = datetime(2026, 11, 7, 14, 30, tzinfo=UTC)
_ON_DEMAND_TOPIC = "onex.cmd.example.on-demand-requested.v1"


def _overlay(
    *, sender: str | None = "node_example_sender_effect", on_demand: bool = True
) -> ModelSeamDeadlineOverlay:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    declarations = [
        {
            "seam": "example-query",
            "trigger": "automatic",
            "required": True,
            "sender": sender,
        }
    ]
    if on_demand:
        raw["seams"].append(
            {
                "name": "example-on-demand",
                "command_topic": _ON_DEMAND_TOPIC,
                "result_topic": "onex.evt.example.on-demand-completed.v1",
                "consumer_group_prefix": "local.example.on_demand.consume.v1.",
                "request": {"query": "holds"},
                "fault_request": {"query": "not-a-query"},
                "error_keys": ["error"],
                "deadline_seconds": 30,
                "on_demand": True,
            }
        )
        declarations.append(
            {"seam": "example-on-demand", "trigger": "on-demand", "required": True}
        )
    raw["seam_declarations"] = declarations
    return ModelSeamDeadlineOverlay.model_validate(raw)


def _node(payload: dict[str, str | int | bool]) -> dict[str, object] | None:
    return {"query": payload["query"], "rows": []}


def _handler(
    overlay: ModelSeamDeadlineOverlay,
    ledger: InMemoryLedger,
    *,
    with_attacher: bool = True,
    latency_ms: int = 5,
) -> tuple[HandlerSeamDeadline, InMemoryNodeAttacher | None]:
    seam = overlay.seams[0]
    bus = InMemorySeamBus({seam.command_topic: _node}, latency_ms=latency_ms)
    attacher = (
        InMemoryNodeAttacher(bus, available={_ON_DEMAND_TOPIC: _node})
        if with_attacher
        else None
    )
    return (
        HandlerSeamDeadline(
            overlay=overlay,
            host="test-host",
            bus=bus,
            appender=ledger,
            clock=lambda: _NOW,
            attacher=attacher,
        ),
        attacher,
    )


def _run(handler: HandlerSeamDeadline) -> list[tuple[str, dict[str, object]]]:
    command = {"correlation_id": "counts-1", "write": True}
    out = answer_command(json.dumps(command).encode(), handler, load_topics())
    return [(topic, json.loads(body)) for topic, body in out]


def test_golden_chain_the_on_demand_seam_is_demanded_and_the_row_carries_counts() -> (
    None
):
    ledger = InMemoryLedger()
    handler, attacher = _handler(_overlay(), ledger)

    events = _run(handler)

    assert events[-1][0] == load_topics()["seam_deadline_run_completed"]
    assert events[-1][1]["verdict"] == "WORKING"
    crossings = {
        body["seam"]: body["crossing"]
        for topic, body in events
        if topic == load_topics()["seam_crossing_read"]
    }
    assert crossings == {"example-query": "result", "example-on-demand": "result"}
    (row,) = ledger.lines
    assert "seams_automatic=1" in row
    assert "seams_on_demand=1" in row
    assert "required_without_sender=0" in row
    assert "seam_example-on-demand=WORKING:result:" in row
    assert attacher is not None
    assert attacher.attached == ()  # the node was attached for the demand only


def test_error_chain_a_required_automatic_seam_with_no_sender_reads_not_working() -> (
    None
):
    ledger = InMemoryLedger()
    handler, _ = _handler(_overlay(sender=None), ledger)

    events = _run(handler)

    assert events[-1][0] == load_topics()["seam_deadline_run_failed"]
    final = events[-1][1]
    assert final["verdict"] == "NOT_WORKING"
    assert final["reasons"] == ["example-query:no-bound-sender"]
    assert final["failure"] == "required-sender-missing:example-query"
    (row,) = ledger.lines
    assert "required_without_sender=1" in row
    assert "verdict=NOT_WORKING" in row
    assert "reason=example-query:no-bound-sender" in row


def test_error_chain_an_on_demand_seam_with_nothing_to_attach_it_reads_not_working() -> (
    None
):
    ledger = InMemoryLedger()
    handler, _ = _handler(_overlay(), ledger, with_attacher=False)

    events = _run(handler)

    final = events[-1][1]
    assert events[-1][0] == load_topics()["seam_deadline_run_failed"]
    assert final["verdict"] == "NOT_WORKING"
    assert final["reasons"] == ["example-on-demand:attach-failed"]
    assert "seam_example-on-demand=NOT_WORKING:attach-failed:" in ledger.lines[0]


def test_error_chain_a_slow_on_demand_node_reads_not_working() -> None:
    ledger = InMemoryLedger()
    handler, attacher = _handler(_overlay(), ledger, latency_ms=31_000)

    events = _run(handler)

    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert "example-on-demand:no-result-by-deadline" in events[-1][1]["reasons"]
    assert attacher is not None
    assert attacher.attached == ()


def test_a_reading_with_no_declarations_keeps_the_row_it_had() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    overlay = ModelSeamDeadlineOverlay.model_validate(raw)
    assert overlay.seam_declarations == ()
    ledger = InMemoryLedger()
    handler, _ = _handler(overlay, ledger, with_attacher=False)

    events = _run(handler)

    assert events[-1][1]["verdict"] == "WORKING"
    assert "seams_automatic" not in ledger.lines[0]
    assert "required_without_sender" not in ledger.lines[0]


def test_the_overlay_hash_of_a_reading_without_on_demand_seams_is_unchanged() -> None:
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    before = ModelSeamDeadlineOverlay.model_validate(raw).overlay_hash()
    raw["seam_declarations"] = []
    after = ModelSeamDeadlineOverlay.model_validate(raw).overlay_hash()
    assert before == after
    # pinned: the hash origin/dev computes for this example overlay
    assert before == "9035a8c48ee36c774850f71c"


def test_a_declaration_for_a_seam_the_overlay_does_not_run_as_a_demand_is_refused() -> (
    None
):
    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    raw["seam_declarations"] = [
        {"seam": "example-query", "trigger": "on-demand", "required": True}
    ]
    with pytest.raises(ValueError, match="on-demand"):
        ModelSeamDeadlineOverlay.model_validate(raw)


def test_the_probe_command_prints_the_seam_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from omnimarket.nodes.node_seam_deadline_probe_effect.runtime_seam_deadline import (
        main,
    )

    raw = yaml.safe_load(_EXAMPLE.read_text(encoding="utf-8"))
    raw["seam_declarations"] = [
        {"seam": "example-query", "trigger": "automatic", "required": True}
    ]
    node_dir = tmp_path / "node_seam_deadline_probe_effect"
    node_dir.mkdir()
    (node_dir / "overlay.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.setenv("EXAMPLE_HOST_NAME", "test-host")

    main(["probe"])

    out = capsys.readouterr().out
    assert (
        "COUNTS automatic=1 on_demand=0 required_without_sender=1 "
        "working=0 not_working=1 unknown=0" in out
    )
    assert (
        "reason=example-query:no-result-by-deadline,example-query:no-bound-sender"
        in out
    )
