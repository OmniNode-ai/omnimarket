# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One suite runs each listed effect on both backends; one-sided effects turn it red (OMN-22911)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_two_backend_suite_effect.adapters.report_sources import (
    FileReportSource,
    InMemoryReportSource,
)
from omnimarket.nodes.node_two_backend_suite_effect.adapters.resolver import (
    ImportResolver,
)
from omnimarket.nodes.node_two_backend_suite_effect.handlers.handler_two_backend_suite import (
    HandlerTwoBackendSuite,
    events_of,
    load_topics,
)
from omnimarket.nodes.node_two_backend_suite_effect.models.model_two_backend_suite import (
    EnumTwoBackendVerdict,
    ModelEffectBackendEntry,
    ModelEffectBackendReport,
    ModelTwoBackendRequest,
)

pytestmark = pytest.mark.integration

PLANTED = "tests.helpers.two_backend_planted"
SRC = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"


def entry(name: str, real: str | None = "make_real") -> ModelEffectBackendEntry:
    return ModelEffectBackendEntry(
        node_name=name,
        path=f"{name}/contract.yaml",
        has_in_memory_default=True,
        has_real_backend_overlay=real is not None,
        in_memory_factory=f"{PLANTED}.make_memory",
        real_factory=f"{PLANTED}.{real}" if real else "",
        overlay_key="store_backend" if real else "",
        scenario=f"{PLANTED}.read_back",
    )


def run(*entries: ModelEffectBackendEntry, tmp_path: Path):
    handler = HandlerTwoBackendSuite(
        source=InMemoryReportSource(ModelEffectBackendReport(entries=entries)),
        resolver=ImportResolver(),
        workdir=tmp_path,
    )
    return handler.handle(ModelTwoBackendRequest(correlation_id="c-1"))


def verdicts(result) -> dict[str, EnumTwoBackendVerdict]:
    return {r.node_name: r.verdict for r in result.runs}


def test_two_backend_effect_passing_on_both_is_green(tmp_path: Path) -> None:
    result = run(entry("node_ok_effect"), tmp_path=tmp_path)
    assert verdicts(result) == {"node_ok_effect": EnumTwoBackendVerdict.BOTH_PASS}
    assert not result.red


def test_two_backend_planted_effect_passing_on_one_backend_only_turns_red(
    tmp_path: Path,
) -> None:
    result = run(
        entry("node_ok_effect"),
        entry("node_down_effect", real="make_real_down"),
        tmp_path=tmp_path,
    )
    assert verdicts(result) == {
        "node_down_effect": EnumTwoBackendVerdict.ONE_BACKEND_ONLY,
        "node_ok_effect": EnumTwoBackendVerdict.BOTH_PASS,
    }
    assert result.red
    down = next(r for r in result.runs if r.node_name == "node_down_effect")
    assert down.in_memory == "pass"
    assert down.real.startswith("fail:ConnectionRefusedError")


def test_two_backend_backends_that_disagree_turn_red(tmp_path: Path) -> None:
    result = run(
        entry("node_split_effect", real="make_real_diverging"), tmp_path=tmp_path
    )
    assert verdicts(result) == {"node_split_effect": EnumTwoBackendVerdict.DIVERGED}
    assert result.red


def test_two_backend_an_effect_not_on_both_is_counted_not_run(tmp_path: Path) -> None:
    result = run(
        entry("node_ok_effect"),
        entry("node_mem_only_effect", real=None),
        tmp_path=tmp_path,
    )
    assert [r.node_name for r in result.runs] == ["node_ok_effect"]
    assert result.not_on_both_count == 1
    assert not result.red


def test_two_backend_an_unresolvable_factory_is_a_failed_backend(
    tmp_path: Path,
) -> None:
    broken = entry("node_ghost_effect").model_copy(
        update={"real_factory": f"{PLANTED}.no_such_factory"}
    )
    result = run(broken, tmp_path=tmp_path)
    assert verdicts(result) == {
        "node_ghost_effect": EnumTwoBackendVerdict.ONE_BACKEND_ONLY
    }


def test_two_backend_golden_chain_events(tmp_path: Path) -> None:
    topics = load_topics()
    result = run(entry("node_ok_effect"), tmp_path=tmp_path)
    events = events_of(result, topics)
    assert [t for t, _ in events] == [
        topics["two_backend_effect_passed"],
        topics["two_backend_suite_completed"],
    ]
    assert all(body["correlation_id"] == "c-1" for _, body in events)
    assert events[-1][1]["failed"] == 0


def test_two_backend_error_chain_events(tmp_path: Path) -> None:
    topics = load_topics()
    result = run(
        entry("node_ok_effect"),
        entry("node_down_effect", real="make_real_down"),
        tmp_path=tmp_path,
    )
    events = events_of(result, topics)
    assert [t for t, _ in events] == [
        topics["two_backend_effect_failed"],
        topics["two_backend_effect_passed"],
        topics["two_backend_suite_completed"],
    ]
    assert events[0][1]["verdict"] == "one_backend_only"
    assert events[-1][1]["failed"] == 1


def test_two_backend_unreadable_report_fails_the_suite_with_a_typed_event(
    tmp_path: Path,
) -> None:
    topics = load_topics()
    handler = HandlerTwoBackendSuite(
        source=FileReportSource(tmp_path / "missing.json"),
        resolver=ImportResolver(),
        workdir=tmp_path,
    )
    result = handler.handle(ModelTwoBackendRequest(correlation_id="c-2"))
    assert result.status == "failed"
    assert result.failure.startswith("report-unreadable")
    assert events_of(result, topics)[0][0] == topics["two_backend_suite_failed"]


def test_two_backend_file_report_in_the_core_validators_shape(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text(
        json.dumps(
            {
                "entries": [entry("node_ok_effect").model_dump()],
                "unreadable": [],
                "effects_total": 1,
                "without_in_memory_default": [],
                "not_on_both": [],
                "on_both": ["node_ok_effect"],
                "not_on_both_count": 0,
            }
        )
    )
    handler = HandlerTwoBackendSuite(
        source=FileReportSource(path), resolver=ImportResolver(), workdir=tmp_path
    )
    result = handler.handle(ModelTwoBackendRequest(correlation_id="c-3"))
    assert verdicts(result) == {"node_ok_effect": EnumTwoBackendVerdict.BOTH_PASS}


def test_two_backend_enrolled_reading_writer_passes_on_both_backends(
    tmp_path: Path,
) -> None:
    contract = yaml.safe_load(
        (SRC / "node_reading_writer_effect" / "contract.yaml").read_text()
    )
    backends = contract["backends"]
    assert backends["default"] == "in_memory"
    real = ModelEffectBackendEntry(
        node_name=contract["node_name"],
        path="node_reading_writer_effect/contract.yaml",
        has_in_memory_default=True,
        has_real_backend_overlay=True,
        in_memory_factory=backends["in_memory"]["factory"],
        real_factory=backends["real"]["factory"],
        overlay_key=backends["real"]["overlay_key"],
        scenario=backends["suite"]["scenario"],
    )
    result = run(real, tmp_path=tmp_path)
    assert verdicts(result) == {
        "node_reading_writer_effect": EnumTwoBackendVerdict.BOTH_PASS
    }


def test_two_backend_declared_on_both_with_no_scenario_is_red(tmp_path: Path) -> None:
    bare = entry("node_bare_effect").model_copy(update={"scenario": ""})
    result = run(bare, tmp_path=tmp_path)
    assert verdicts(result) == {"node_bare_effect": EnumTwoBackendVerdict.BOTH_FAIL}
    assert result.red


def test_two_backend_status_row_carries_red_and_the_count_and_passes_the_grammar(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from omnimarket.handlers import ledger_row_grammar as grammar
    from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
        evidence_row_from_text,
    )
    from omnimarket.nodes.node_two_backend_suite_effect.handlers.handler_two_backend_suite import (
        build_status_row,
    )

    at = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
    red = run(
        entry("node_down_effect", real="make_real_down"),
        entry("node_mem_only_effect", real=None),
        tmp_path=tmp_path,
    )
    row = build_status_row(red, lane="suite-lane", actor="node:suite", at=at)
    assert grammar.row_refusals(row) == [], row
    cells = evidence_row_from_text(row).cells
    assert (cells["lane"], cells["red"], cells["not_on_both"], cells["run"]) == (
        "suite-lane",
        "1",
        "1",
        "1",
    )
    green = run(entry("node_ok_effect"), tmp_path=tmp_path)
    assert (
        evidence_row_from_text(
            build_status_row(green, lane="l", actor="a", at=at)
        ).cells["red"]
        == "0"
    )
    failed = HandlerTwoBackendSuite(
        source=FileReportSource(tmp_path / "missing.json"),
        resolver=ImportResolver(),
        workdir=tmp_path,
    ).handle(ModelTwoBackendRequest(correlation_id="c-9"))
    assert (
        evidence_row_from_text(
            build_status_row(failed, lane="l", actor="a", at=at)
        ).cells["red"]
        == "unread"
    )
