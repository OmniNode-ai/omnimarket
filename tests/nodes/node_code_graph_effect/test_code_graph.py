# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code graph on both backends, its row, and the piece 1 reading it feeds (OMN-22651).

The last tests are the reading's failing controls: stop the timer and advance the clock and the
reading goes NOT WORKING; drop the contract links or the entry points in a drill and the row goes NOT WORKING
while the operational reading does not read it.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.models.model_code_graph import EnumGraphFault, ModelCodeGraphCommand
from omnimarket.models.model_reading import (
    EnumReadingVerdict,
    ModelPieceReadingSpec,
    ModelReadingComputeRequest,
    ModelReadingSourceRows,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import (
    GraphRules,
    build_graph,
    glob_regex,
)
from omnimarket.nodes.node_code_graph_effect.protocols import ProtocolRepoSource
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    HandlerReadingCompute,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    APP,
    HOST,
    REF,
    RUN_AT,
    overlay,
    source,
)

pytestmark = pytest.mark.unit

#: The piece 1 reading as the reading writer's overlay declares it (the overlay home holds the live copy).
PIECE_1 = ModelPieceReadingSpec.model_validate(
    {
        "piece": 1,
        "name": "code graph",
        "readable_today": True,
        "basis": "nightly-graph-row",
        "sources": [
            {
                "source_id": "nightly",
                "row_type": "STATUS",
                "lane": "code-graph",
                "cells": {"src": "code-graph", "drill": "no"},
                "mode": "newest",
                "pass_when": [{"cell": "verdict", "op": "eq", "value": "WORKING"}],
                "max_age_seconds": 93600,
            }
        ],
        "injected_fault": "Stop the timer and advance the clock",
    }
)


@pytest.fixture(params=["memory", "git"])
def repos(request: pytest.FixtureRequest, tmp_path: Path) -> ProtocolRepoSource:
    return source(request.param, tmp_path)


def _handler(repos: ProtocolRepoSource, ledger: InMemoryLedger | None) -> CodeGraphRun:
    return CodeGraphRun(
        overlay=overlay(),
        host=HOST,
        source=repos,
        appender=ledger,
        clock=lambda: RUN_AT,
    )


def _reading(ledger: InMemoryLedger, *, hours_after_run: float) -> EnumReadingVerdict:
    window_end = RUN_AT + timedelta(hours=hours_after_run)
    rows = tuple(row for line in ledger.lines if (row := evidence_row_from_text(line)))
    result = HandlerReadingCompute().handle(
        ModelReadingComputeRequest(
            spec=PIECE_1,
            window_start=window_end - timedelta(hours=1),
            window_end=window_end,
            cutoff=window_end + timedelta(minutes=10),
            order_hash="o",
            overlay_hash="h",
            revision="1",
            sources=(ModelReadingSourceRows(source_id="nightly", rows=rows),),
        )
    )
    return result.verdict


def test_glob_spans_directories_only_with_double_star() -> None:
    assert glob_regex("**/tests/**").match("src/a/tests/test_x.py")
    assert glob_regex("tests/**").match("tests/a/b.py")
    assert glob_regex("scripts/*.py").match("scripts/run.py")
    assert not glob_regex("scripts/*.py").match("scripts/sub/run.py")


def test_graph_reaches_roots_across_repositories(repos: ProtocolRepoSource) -> None:
    rules = GraphRules(roots=overlay().graph_roots)
    snapshots = [
        repos.snapshot(name, REF, rules.wanted)
        for name in ("example_core", "example_app")
    ]
    graph = build_graph(snapshots, rules)
    core = graph.repo_graph("example_core", snapshots[0].head)
    app = graph.repo_graph("example_app", snapshots[1].head)
    # a TYPE_CHECKING import loads nothing, but marks the lazy export the CLI imports
    assert core.unreachable == (
        "src/example_core/helpers.py",
        "src/example_core/lazy/model_beta.py",
        "src/example_core/unused.py",
    )
    assert ("example_core", "src/example_core/lazy/model_alpha.py") in graph.reached
    assert core.test_only == ("src/example_core/helpers.py",)
    assert app.unreachable == ("src/example_app/orphan.py",)
    # the handler's import of the other repository reaches it, and the script's dynamic import counts
    assert ("example_core", "src/example_core/models.py") in graph.reached
    assert ("example_app", "src/example_app/dyn.py") in graph.reached
    # a module a workflow runs with -m is a root
    assert ("example_core", "src/example_core/ci_only.py") in graph.reached
    # the contract links the registered node package to its handler and the handler to util
    assert ("example_app", "src/example_app/nodes/node_a/util.py") in graph.reached
    assert core.test_modules == 1


def test_nightly_writes_one_working_row(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    result = _handler(repos, ledger).handle(ModelCodeGraphCommand(correlation_id="n1"))
    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.WORKING, result.reasons
    assert result.ledger == "written"
    assert len(ledger.lines) == 1
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.lane == "code-graph"
    assert row.cells["src"] == "code-graph"
    assert row.cells["drill"] == "no"
    assert row.cells["verdict"] == "WORKING"
    assert row.cells["sample"] == "7/7"
    assert row.cells["controls"] == "2/2"
    assert row.cells["unreachable"] == "4"


def test_probe_appends_nothing(repos: ProtocolRepoSource) -> None:
    result = _handler(repos, None).handle(
        ModelCodeGraphCommand(correlation_id="p", write=False)
    )
    assert result.ledger == "not-written"
    assert result.verdict is EnumReadingVerdict.WORKING


def test_unreadable_repository_fails_the_run_with_a_not_working_row(
    repos: ProtocolRepoSource,
) -> None:
    ledger = InMemoryLedger()
    result = _handler(repos, ledger).handle(
        ModelCodeGraphCommand(correlation_id="u", fault=EnumGraphFault.UNREADABLE_REPO)
    )
    assert result.status == "failed"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome.value == "source-unreadable"
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["drill"] == "yes"
    assert row.cells["verdict"] == "NOT_WORKING"


def test_append_that_does_not_land_fails_the_run(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    result = _handler(repos, ledger).handle(ModelCodeGraphCommand(correlation_id="a"))
    assert result.status == "failed"
    assert result.ledger == "append-failed:refused"
    assert result.failure == "append-failed:refused:injected-append-fault"


def test_a_sample_changed_after_freezing_refuses_the_load() -> None:
    document = overlay().model_dump(mode="json")
    document["sample"]["min_agree"] = 5
    with pytest.raises(ValidationError, match="frozen hash"):
        overlay(sample=document["sample"])


def test_reading_is_working_after_a_nightly_row(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    _handler(repos, ledger).handle(ModelCodeGraphCommand(correlation_id="r"))
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.WORKING


def test_failing_control_stop_the_timer_and_advance_the_clock(
    repos: ProtocolRepoSource,
) -> None:
    """The plan's injected fault: no new nightly row, the clock moves past the bound."""
    ledger = InMemoryLedger()
    _handler(repos, ledger).handle(ModelCodeGraphCommand(correlation_id="t"))
    assert _reading(ledger, hours_after_run=27) is EnumReadingVerdict.NOT_WORKING


def test_failing_control_drop_the_contract_links(repos: ProtocolRepoSource) -> None:
    """A drill that drops the contract links: the row is NOT WORKING and is drill only."""
    ledger = InMemoryLedger()
    result = _handler(repos, ledger).handle(
        ModelCodeGraphCommand(
            correlation_id="d", fault=EnumGraphFault.DROP_CONTRACT_LINKS
        )
    )
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any(reason.startswith("control-unreached") for reason in result.reasons)
    # the operational reading reads no drill row, so with only this row it is stale
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.NOT_WORKING


def test_failing_control_drop_the_entry_points_fails_the_reading(
    repos: ProtocolRepoSource,
) -> None:
    """The break that fails the real reading (OMN-22651): on the live sample dropping the contract links leaves
    28 of 30 against a bar of 27, so it cannot fail it; dropping the pyproject entry points leaves 23 of 30."""
    ledger = InMemoryLedger()
    result = _handler(repos, ledger).handle(
        ModelCodeGraphCommand(
            correlation_id="e", fault=EnumGraphFault.DROP_ENTRY_POINTS
        )
    )
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0].startswith("below-bar")
    assert result.sample is not None
    assert result.sample.agree < overlay().sample.min_agree
    # the clean run on the same repositories passes, so it is the break and not the fixture
    clean = _handler(repos, InMemoryLedger()).handle(
        ModelCodeGraphCommand(correlation_id="c")
    )
    assert clean.verdict is EnumReadingVerdict.WORKING
    # a drill row is not read by the operational reading
    assert _reading(ledger, hours_after_run=2) is EnumReadingVerdict.NOT_WORKING


def test_an_orphan_handler_breaks_the_sample(tmp_path: Path) -> None:
    """The handler stops importing util: util is unreached and the sample falls below its bar."""
    app = dict(APP)
    app["src/example_app/nodes/node_a/handler.py"] = "class Handler:\n    pass\n"
    app["scripts/run.py"] = "VALUE = 1\n"
    ledger = InMemoryLedger()
    result = _handler(source("memory", tmp_path, app), ledger).handle(
        ModelCodeGraphCommand(correlation_id="o")
    )
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.reasons[0].startswith("below-bar")
