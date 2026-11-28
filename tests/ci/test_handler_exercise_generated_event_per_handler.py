# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every handler of the code graph has a generated exercise event or a named reason (OMN-26337, AC1).

The rows come from the code graph's own ``handlers`` question over this repository's checkout;
the handler list is never written here. The offender check is run against the real graph and,
as a control, against a result with one row removed, which it must report.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from omnimarket.models.model_code_graph_handlers import (
    EnumHandlerReference,
    EnumHandlerRoute,
    ModelHandlerRow,
)
from omnimarket.nodes.node_code_graph_effect.adapters.repo_sources import (
    InMemoryRepoSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_index import (
    build_version,
)
from omnimarket.nodes.node_code_graph_effect.handlers.graph_compute import GraphRules
from omnimarket.nodes.node_handler_exercise_generator_compute.handlers.handler_handler_exercise_generator import (
    HandlerHandlerExerciseGenerator,
)
from omnimarket.nodes.node_handler_exercise_generator_compute.models.model_handler_exercise import (
    EnumNoEventReason,
    EnumPayloadSource,
    ModelHandlerExerciseRequest,
    ModelHandlerExerciseResult,
)
from tests.nodes.node_code_graph_effect.code_graph_support import overlay

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _graph_rows() -> tuple[ModelHandlerRow, ...]:
    rules = GraphRules(roots=overlay().graph_roots)
    files = {
        str(p.relative_to(ROOT)): p.read_text(encoding="utf-8")
        for p in (ROOT / "src").rglob("*")
        if p.is_file()
        and p.suffix in {".py", ".yaml"}
        and rules.wanted(str(p.relative_to(ROOT)))
    }
    snapshot = InMemoryRepoSource({"omnimarket": {"HEAD": ("0" * 40, files)}}).snapshot(
        "omnimarket", "HEAD", rules.wanted
    )
    return build_version(snapshot, rules).handler_rows


def _identities(rows: Iterable[ModelHandlerRow]) -> set[tuple[str, str]]:
    return {(row.contract, row.operation) for row in rows}


def handlers_with_no_generated_event(
    rows: tuple[ModelHandlerRow, ...], result: ModelHandlerExerciseResult
) -> list[tuple[str, str]]:
    """Rows of the graph that are neither an event nor a named gap in ``result``."""
    accounted = {(e.contract, e.operation) for e in result.events} | {
        (g.contract, g.operation) for g in result.without_event
    }
    return sorted(_identities(rows) - accounted)


@pytest.fixture(scope="module")
def rows() -> tuple[ModelHandlerRow, ...]:
    return _graph_rows()


@pytest.fixture(scope="module")
def result(rows: tuple[ModelHandlerRow, ...]) -> ModelHandlerExerciseResult:
    return HandlerHandlerExerciseGenerator().handle(
        ModelHandlerExerciseRequest(rows=rows)
    )


def test_generated_event_per_handler_covers_every_graph_row(
    rows: tuple[ModelHandlerRow, ...], result: ModelHandlerExerciseResult
) -> None:
    assert len(rows) > 100, (
        "the graph read nothing: the control would pass on an empty set"
    )
    assert handlers_with_no_generated_event(rows, result) == []


def test_generated_event_per_handler_one_event_per_routed_resolved_input(
    rows: tuple[ModelHandlerRow, ...], result: ModelHandlerExerciseResult
) -> None:
    assert len({e.event_id for e in result.events}) == len(result.events)
    event_ids = {(e.contract, e.operation) for e in result.events}
    for row in rows:
        if (
            not any(
                u.reference is not EnumHandlerReference.OUTPUT_MODEL
                for u in row.unresolved
            )
            and row.route is not EnumHandlerRoute.NO_ROUTE
        ):
            assert (row.contract, row.operation) in event_ids or any(
                (g.contract, g.operation) == (row.contract, row.operation)
                and g.reason
                in {
                    EnumNoEventReason.INPUT_MODEL_NOT_IMPORTABLE,
                    EnumNoEventReason.INPUT_MODEL_NOT_A_MODEL,
                    EnumNoEventReason.PAYLOAD_NOT_BUILDABLE,
                }
                for g in result.without_event
            ), (row.contract, row.operation)
    assert result.events, "no event was generated from the real graph"
    assert {e.payload_source for e in result.events} >= {
        EnumPayloadSource.INPUT_MODEL_SCHEMA
    }


def test_generated_event_per_handler_reports_a_row_with_no_event(
    rows: tuple[ModelHandlerRow, ...], result: ModelHandlerExerciseResult
) -> None:
    victim = result.events[0]
    dropped = ModelHandlerExerciseResult(
        events=tuple(e for e in result.events if e is not victim),
        without_event=result.without_event,
    )
    assert handlers_with_no_generated_event(rows, dropped) == [
        (victim.contract, victim.operation)
    ]
