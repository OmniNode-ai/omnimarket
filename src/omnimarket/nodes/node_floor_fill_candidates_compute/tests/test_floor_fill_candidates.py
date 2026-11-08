# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_floor_fill_candidates_compute: the next startable task per line, from the published floor record."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.models import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.tests.support import (
    LINE,
    MAX_AGE,
    NOW,
    READY_10_40,
    WINDOW,
    all_ready_payloads_ok,
    all_ready_tickets_usable,
    fixture_text,
    item,
    key,
    minutes_before,
    payload_ok,
    record,
    request,
    reserved,
    sent,
    usable,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parent.parent


def run(req: ModelFloorFillCandidatesRequest) -> ModelFloorFillCandidatesResult:
    return HandlerFloorFillCandidates().handle(req)


def only_line(result: ModelFloorFillCandidatesResult) -> ModelFloorFillLine:
    [line] = result.lines
    return line


def table(
    result: ModelFloorFillCandidatesResult,
) -> dict[str, tuple[str, tuple[tuple[str, str], ...]]]:
    return {
        line.line_id: (
            line.verdict,
            tuple((c.task_id, c.verdict) for c in line.candidates),
        )
        for line in result.lines
    }


# ------------------------------------------------------------------ AC1: the 10:40Z record


def test_fixture_verdicts_startable_for_all_six_lines() -> None:
    result = run(request(ticket_verdicts=all_ready_tickets_usable()))
    assert result.verdict == "READ"
    assert table(result) == {
        line: ("STARTABLE", tuple((task, "STARTABLE") for task in tasks))
        for line, tasks in READY_10_40.items()
    }
    for line in result.lines:
        assert line.candidates[0].task_id == READY_10_40[line.line_id][0]
        assert all(c.key == key(line.line_id, c.task_id) for c in line.candidates)
        assert all(c.ticket.startswith("TCK-") for c in line.candidates)
        assert line.untickettable == ()


def test_fixture_verdicts_needs_ticket_when_no_entry_ticket_and_payload_ok() -> None:
    result = run(request(payload_verdicts=all_ready_payloads_ok()))
    assert table(result) == {
        line: ("NEEDS-TICKET", tuple((task, "NEEDS-TICKET") for task in tasks))
        for line, tasks in READY_10_40.items()
    }
    assert all(c.ticket == "" for line in result.lines for c in line.candidates)


def test_fixture_not_ready_causes_are_listed_per_line() -> None:
    result = run(request(ticket_verdicts=all_ready_tickets_usable()))
    by_line = {line.line_id: dict(line.not_ready_pairs()) for line in result.lines}
    assert by_line["2026-10-01-line-a-plan"] == {"TE.8": "active"}
    assert by_line["2026-10-02-line-b-plan"] == {"CM.2": "active", "CM.3": "done"}
    assert by_line["2026-10-05-line-e-plan"] == {
        "G0": "not-a-lane:gate",
        "P1": "not-a-lane:adopted",
        "G1a": "not-a-lane:gate",
        "W1.1": "wait:G1a",
        "G1b": "not-a-lane:gate",
    }


# ------------------------------------------------------------------ AC2: the pill is never read


@pytest.mark.parametrize("pill", ["building", "stopped", "queued"])
def test_pill_never_changes_a_verdict(pill: str) -> None:
    baseline = run(request(ticket_verdicts=all_ready_tickets_usable()))
    rewritten = json.loads(fixture_text())
    for line in rewritten["lines"]:
        line["status_pill"] = pill
    result = run(
        request(
            floor_record=json.dumps(rewritten),
            ticket_verdicts=all_ready_tickets_usable(),
        )
    )
    assert result.model_dump_json() == baseline.model_dump_json()


# ------------------------------------------------------------------ AC3: open PR and held keys


def three_tasks() -> str:
    return record(
        [
            item("T0.1"),
            item("T0.2", prs=("example/repo#9",)),
            item("T0.3"),
            item("T0.4"),
        ]
    )


def tickets(*tasks: str) -> tuple[ModelFloorEntryTicketVerdict, ...]:
    return tuple(usable(LINE, t, f"TCK-{t}") for t in tasks)


def test_open_pr_and_held_key_are_never_returned_while_capacity_remains() -> None:
    result = run(
        request(
            floor_record=three_tasks(),
            max_fill_lanes_per_line=2,
            ticket_verdicts=tickets("T0.1", "T0.2", "T0.3", "T0.4"),
            entries=(reserved(LINE, "T0.1"),),
        )
    )
    line = only_line(result)
    assert line.verdict == "STARTABLE"
    assert [c.task_id for c in line.candidates] == ["T0.3", "T0.4"]
    causes = dict(line.not_ready_pairs())
    assert causes["T0.2"] == "open-pr:example/repo#9"
    assert causes["T0.1"] == "held:RESERVED"


def test_line_is_at_capacity_when_capacity_is_used_up_and_returns_none() -> None:
    result = run(
        request(
            floor_record=three_tasks(),
            max_fill_lanes_per_line=1,
            ticket_verdicts=tickets("T0.1", "T0.2", "T0.3", "T0.4"),
            entries=(reserved(LINE, "T0.1"),),
        )
    )
    line = only_line(result)
    assert line.verdict == "AT-CAPACITY"
    assert line.candidates == ()
    assert line.capacity_left == 0


def test_windowed_sent_entry_holds_its_key_and_an_expired_one_does_not() -> None:
    inside = run(
        request(
            floor_record=three_tasks(),
            max_fill_lanes_per_line=3,
            ticket_verdicts=tickets("T0.1", "T0.3", "T0.4"),
            entries=(
                sent(LINE, "T0.1", at=minutes_before(NOW, WINDOW - 1), ended=True),
            ),
        )
    )
    assert [c.task_id for c in only_line(inside).candidates] == ["T0.3", "T0.4"]
    outside = run(
        request(
            floor_record=three_tasks(),
            max_fill_lanes_per_line=3,
            ticket_verdicts=tickets("T0.1", "T0.3", "T0.4"),
            entries=(
                sent(LINE, "T0.1", at=minutes_before(NOW, WINDOW + 1), ended=True),
            ),
        )
    )
    assert [c.task_id for c in only_line(outside).candidates] == [
        "T0.1",
        "T0.3",
        "T0.4",
    ]


def test_live_lane_naming_a_task_keeps_it_out() -> None:
    lanes: list[dict[str, Any]] = [
        {
            "lane": "lane-1",
            "station": {"line_id": LINE, "station_id": "tasks", "item_id": "T0.1"},
        },
        {"lane": "lane-2", "station": None},
    ]
    result = run(
        request(
            floor_record=record([item("T0.1"), item("T0.3")], lanes=lanes),
            ticket_verdicts=tickets("T0.1", "T0.3"),
        )
    )
    line = only_line(result)
    assert [c.task_id for c in line.candidates] == ["T0.3"]
    assert dict(line.not_ready_pairs())["T0.1"] == "live-lane:lane-1"


# ------------------------------------------------------------------ AC4: an unread floor


def test_stale_absent_and_malformed_records_are_one_floor_unread_verdict() -> None:
    stale = record([item("T0.1")], generated_at=minutes_before(NOW, MAX_AGE + 1))
    cases = {
        "old": stale,
        "absent": None,
        "malformed": "{this is not json",
    }
    for cause, text in cases.items():
        result = run(
            request(
                floor_record=text,
                ticket_verdicts=tickets("T0.1"),
            )
        )
        assert result.verdict == "FLOOR-UNREAD"
        assert result.unread_cause == cause
        assert result.lines == ()


@pytest.mark.parametrize("text", ["[]", '{"lines": 3}', "", '{"generated_at": "x"}'])
def test_record_of_the_wrong_shape_is_unread_not_empty(text: str) -> None:
    result = run(request(floor_record=text))
    assert result.verdict == "FLOOR-UNREAD"
    assert result.lines == ()


def test_record_exactly_max_age_old_is_still_read() -> None:
    result = run(
        request(
            floor_record=record(
                [item("T0.1")], generated_at=minutes_before(NOW, MAX_AGE)
            ),
            ticket_verdicts=tickets("T0.1"),
        )
    )
    assert result.verdict == "READ"


# ------------------------------------------------------------------ AC5: each cause is its own verdict


UNTICKETABLE_CASES: dict[str, dict[str, Any]] = {
    "needs-spec:acceptance,files": {
        "payload_verdicts": (
            ModelFloorTaskPayloadVerdict(
                line_id=LINE,
                task_id="T0.1",
                ok=False,
                cause="needs-spec:acceptance,files",
            ),
        )
    },
    "no-parent": {
        "payload_verdicts": (
            ModelFloorTaskPayloadVerdict(
                line_id=LINE, task_id="T0.1", ok=False, cause="no-parent"
            ),
        )
    },
    "plan-unread": {
        "payload_verdicts": (
            ModelFloorTaskPayloadVerdict(
                line_id=LINE, task_id="T0.1", ok=False, cause="plan-unread"
            ),
        )
    },
    "plan-unread-absent-payload-verdict": {"payload_verdicts": ()},
    "ticket-refused:canceled": {
        "ticket_verdicts": (
            ModelFloorEntryTicketVerdict(
                line_id=LINE, task_id="T0.1", status="refused", refusal="canceled"
            ),
        )
    },
    "ticket-refused:done": {
        "ticket_verdicts": (
            ModelFloorEntryTicketVerdict(
                line_id=LINE, task_id="T0.1", status="refused", refusal="done"
            ),
        )
    },
    "ticket-refused:umbrella": {
        "ticket_verdicts": (
            ModelFloorEntryTicketVerdict(
                line_id=LINE, task_id="T0.1", status="refused", refusal="umbrella"
            ),
        )
    },
    "ticket-refused:old-fill-visible": {
        "ticket_verdicts": (
            ModelFloorEntryTicketVerdict(
                line_id=LINE,
                task_id="T0.1",
                status="refused",
                refusal="old-fill-visible",
            ),
        )
    },
    "ticket-unreadable": {
        "ticket_verdicts": (
            ModelFloorEntryTicketVerdict(
                line_id=LINE, task_id="T0.1", status="unreadable"
            ),
        )
    },
}


@pytest.mark.parametrize("name", sorted(UNTICKETABLE_CASES))
def test_dispatch_verdicts_each_cause_is_its_own_verdict_and_never_a_send(
    name: str,
) -> None:
    result = run(
        request(floor_record=record([item("T0.1")]), **UNTICKETABLE_CASES[name])
    )
    line = only_line(result)
    expected = name.removesuffix("-absent-payload-verdict")
    assert line.verdict == "UNTICKETABLE"
    assert line.candidates == ()
    assert [(u.task_id, u.cause) for u in line.untickettable] == [("T0.1", expected)]


def test_dispatch_verdicts_entry_ticket_wins_over_payload_verdict() -> None:
    result = run(
        request(
            floor_record=record([item("T0.1")]),
            ticket_verdicts=tickets("T0.1"),
            payload_verdicts=(
                ModelFloorTaskPayloadVerdict(
                    line_id=LINE, task_id="T0.1", ok=False, cause="no-parent"
                ),
            ),
        )
    )
    assert [c.verdict for c in only_line(result).candidates] == ["STARTABLE"]


def test_node_holds_no_ticket_id_and_no_umbrella_list() -> None:
    pattern = re.compile(r"OMN-[0-9]+")
    offenders = [
        str(path) for path in NODE_DIR.rglob("*.py") if pattern.search(path.read_text())
    ]
    assert offenders == []


# ------------------------------------------------------------------ AC6: capacity net of entries


def many(n: int) -> str:
    return record([item(f"T{i}") for i in range(1, n + 1)])


def test_capacity_net_of_entries_reserved_and_unended_sent_consume_one_each() -> None:
    base: dict[str, Any] = {
        "floor_record": many(4),
        "ticket_verdicts": tickets("T1", "T2", "T3", "T4"),
        "max_fill_lanes_per_line": 2,
    }
    one = run(request(entries=(reserved(LINE, "T1"),), **base))
    assert only_line(one).capacity_left == 1
    assert [c.task_id for c in only_line(one).candidates] == ["T2", "T3", "T4"]
    one_sent = run(request(entries=(sent(LINE, "T1"),), **base))
    assert only_line(one_sent).capacity_left == 1
    two = run(request(entries=(reserved(LINE, "T1"), sent(LINE, "T2")), **base))
    assert only_line(two).verdict == "AT-CAPACITY"
    assert only_line(two).candidates == ()


def test_capacity_net_of_entries_an_ended_entry_consumes_none_but_holds_its_key() -> (
    None
):
    result = run(
        request(
            floor_record=many(3),
            ticket_verdicts=tickets("T1", "T2", "T3"),
            max_fill_lanes_per_line=1,
            entries=(sent(LINE, "T1", ended=True),),
        )
    )
    line = only_line(result)
    assert line.capacity_left == 1
    assert [c.task_id for c in line.candidates] == ["T2", "T3"]
    assert dict(line.not_ready_pairs())["T1"] == "held:SENT"


def test_capacity_net_of_entries_counts_only_the_entries_of_the_line() -> None:
    result = run(
        request(
            floor_record=many(2),
            ticket_verdicts=tickets("T1", "T2"),
            max_fill_lanes_per_line=1,
            entries=(reserved("another-line", "T1"),),
        )
    )
    assert only_line(result).capacity_left == 1


def test_no_cutoff_by_capacity_every_ready_task_is_returned_beside_the_untickettable() -> (
    None
):
    result = run(
        request(
            floor_record=many(3),
            max_fill_lanes_per_line=1,
            ticket_verdicts=(
                ModelFloorEntryTicketVerdict(
                    line_id=LINE, task_id="T1", status="refused", refusal="canceled"
                ),
                usable(LINE, "T2", "TCK-2"),
                usable(LINE, "T3", "TCK-3"),
            ),
        )
    )
    line = only_line(result)
    assert line.verdict == "STARTABLE"
    assert [c.task_id for c in line.candidates] == ["T2", "T3"]
    assert [(u.task_id, u.cause) for u in line.untickettable] == [
        ("T1", "ticket-refused:canceled")
    ]


# ------------------------------------------------------------------ not ready, and line verdicts


def test_no_startable_lists_the_cause_of_every_task_that_is_not_ready() -> None:
    result = run(
        request(
            floor_record=record(
                [
                    item("A", "done", "PRs example/repo#1", prs=("example/repo#1",)),
                    item("B", "wait", "waits for A,C"),
                    item("C", "active", "PRs example/repo#2", prs=("example/repo#2",)),
                    item("D", "stop", "lane ended failed"),
                    item("E", "prep", "blocked on review"),
                ]
            )
        )
    )
    line = only_line(result)
    assert line.verdict == "NO-STARTABLE"
    assert dict(line.not_ready_pairs()) == {
        "A": "done",
        "B": "wait:A,C",
        "C": "active",
        "D": "stop",
        "E": "not-ready:blocked on review",
    }


def test_the_live_floor_state_integrating_is_read_and_is_not_ready() -> None:
    """The floor publishes ``integrating`` for an item whose PR is landing: it reads, never malformed."""
    result = run(
        request(
            floor_record=record(
                [
                    item(
                        "A",
                        "integrating",
                        "PRs example/repo#1",
                        prs=("example/repo#1",),
                    ),
                    item("B", "prep", "ready"),
                ]
            )
        )
    )
    assert result.verdict != "FLOOR-UNREAD"
    assert result.unread_cause == ""
    line = only_line(result)
    assert "A" in dict(line.not_ready_pairs())
    assert "A" not in [c.task_id for c in line.candidates]


def test_line_without_stations_is_no_startable() -> None:
    text = json.dumps(
        {
            "schema_id": "omni.factory-floor/1",
            "generated_at": "2026-11-02T10:40:00Z",
            "lines": [{"line_id": "empty", "name": "Empty", "stations": []}],
            "floor": {"lanes": []},
        }
    )
    assert only_line(run(request(floor_record=text))).verdict == "NO-STARTABLE"


def test_needs_ticket_and_startable_mix_is_ordered_by_record() -> None:
    result = run(
        request(
            floor_record=many(3),
            ticket_verdicts=tickets("T2"),
            payload_verdicts=(payload_ok(LINE, "T1"), payload_ok(LINE, "T3")),
        )
    )
    line = only_line(result)
    assert [(c.task_id, c.verdict) for c in line.candidates] == [
        ("T1", "NEEDS-TICKET"),
        ("T2", "STARTABLE"),
        ("T3", "NEEDS-TICKET"),
    ]
    assert line.verdict == "NEEDS-TICKET"


# ------------------------------------------------------------------ AC7: determinism and purity


def test_same_input_twice_is_byte_identical() -> None:
    req = request(
        ticket_verdicts=all_ready_tickets_usable(),
        entries=(reserved("2026-10-01-line-a-plan", "T0.1"),),
    )
    assert run(req).model_dump_json() == run(req).model_dump_json()


def test_handler_reads_no_clock_and_does_no_io() -> None:
    source = (NODE_DIR / "handlers" / "handler_floor_fill_candidates.py").read_text()
    for forbidden in (
        "datetime.now",
        "utcnow",
        "time.time",
        "import os",
        "import subprocess",
        "open(",
        "read_text",
        "import socket",
    ):
        assert forbidden not in source, forbidden


# ------------------------------------------------------------------ AC8 (G-NODE): contract, keys, seams


def test_contract_loads_with_archetype_models_routing_and_descriptor() -> None:
    import importlib

    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_floor_fill_candidates_compute"
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["node_archetype"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    [route] = contract["handler_routing"]["handlers"]
    for dotted in (route["input_model"], route["output_model"]):
        module, _, name = dotted.rpartition(".")
        assert getattr(importlib.import_module(module), name)
    module = importlib.import_module(route["handler"]["module"])
    assert getattr(module, route["handler"]["name"]) is HandlerFloorFillCandidates
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == contract["name"]
    assert metadata["capabilities"]["side_effect_class"] == "none"


def test_contract_declares_the_overlay_keys_the_node_reads() -> None:
    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert sorted(contract["deployment_overlay"]["fields"]) == [
        "floor_fill.max_age_minutes",
        "floor_fill.max_fill_lanes_per_line",
        "floor_fill.sent_window_minutes",
        "floor_fill.stopped_continue_rule",
        "floor_fill.stopped_max_lanes",
        "floor_fill.stopped_report_chars",
    ]


def test_seams_in_spi_each_injected_protocol_resolves_into_omnibase_spi() -> None:
    import importlib

    import yaml

    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    listed = []
    for dep in contract.get("protocol_dependencies") or []:
        module, _, name = dep["protocol"].partition(":")
        resolved = getattr(importlib.import_module(module), name)
        listed.append((dep["name"], resolved.__module__))
    assert all(module.startswith("omnibase_spi") for _, module in listed), listed
    # A pure compute injects nothing: its handler takes no constructor argument.
    assert listed == []
    HandlerFloorFillCandidates()


def test_config_variation_max_age_minutes_changes_what_is_read() -> None:
    text = record([item("T0.1")], generated_at=minutes_before(NOW, 20))
    for limit, verdict in ((30, "READ"), (10, "FLOOR-UNREAD")):
        result = run(
            request(
                floor_record=text,
                max_age_minutes=limit,
                ticket_verdicts=tickets("T0.1"),
            )
        )
        assert result.verdict == verdict, limit


def test_config_variation_max_fill_lanes_per_line_changes_capacity() -> None:
    for limit, verdict in ((1, "AT-CAPACITY"), (2, "STARTABLE")):
        result = run(
            request(
                floor_record=many(2),
                max_fill_lanes_per_line=limit,
                ticket_verdicts=tickets("T1", "T2"),
                entries=(reserved(LINE, "T1"),),
            )
        )
        assert only_line(result).verdict == verdict, limit


def test_config_variation_sent_window_minutes_changes_what_is_held() -> None:
    entries = (sent(LINE, "T1", at=minutes_before(NOW, 30), ended=True),)
    for window, held in ((60, True), (10, False)):
        result = run(
            request(
                floor_record=many(2),
                sent_window_minutes=window,
                ticket_verdicts=tickets("T1", "T2"),
                entries=entries,
            )
        )
        ids = [c.task_id for c in only_line(result).candidates]
        assert ("T1" in ids) is (not held), window


@pytest.mark.parametrize(
    "missing",
    ["max_age_minutes", "max_fill_lanes_per_line", "sent_window_minutes"],
)
def test_missing_key_refused_naming_the_overlay_key(missing: str) -> None:
    fields: dict[str, Any] = {
        "now": NOW,
        "floor_record": fixture_text(),
        "max_age_minutes": MAX_AGE,
        "max_fill_lanes_per_line": 2,
        "sent_window_minutes": WINDOW,
    }
    del fields[missing]
    with pytest.raises(ValidationError, match=f"floor_fill.{missing}"):
        ModelFloorFillCandidatesRequest(**fields)


@pytest.mark.parametrize(
    "bad",
    [
        {"max_age_minutes": 0},
        {"max_fill_lanes_per_line": 0},
        {"sent_window_minutes": 0},
        {"now": "not a time"},
    ],
)
def test_nonsense_values_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        request(**bad)
