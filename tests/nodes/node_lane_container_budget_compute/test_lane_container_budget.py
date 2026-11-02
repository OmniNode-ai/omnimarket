# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane's live application containers against its declared budget (one-runtime plan, task T2).

The fixture is a recorded container listing of a lab host whose ``alpha`` lane runs three application containers
beside its message bus and database. The budget is data the caller passes in; none of these tests depends on a count
written in the handler.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_lane_container_budget_compute.handlers.handler_lane_container_budget import (
    HandlerLaneContainerBudget,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lane_container_budget_compute/contract.yaml"
)
HOST = "host-a"
INFRASTRUCTURE = ("redpanda", "postgres")


def declaration(
    lane: str, project: str, budget: int
) -> ModelLaneContainerBudgetDeclaration:
    return ModelLaneContainerBudgetDeclaration(
        host=HOST,
        lane=lane,
        compose_project=project,
        application_container_budget=budget,
        infrastructure_markers=INFRASTRUCTURE,
    )


def judge(
    docker_ps: str,
    *declarations: ModelLaneContainerBudgetDeclaration,
    read_error: str = "",
    host: str = HOST,
) -> ModelLaneContainerBudgetVerdict:
    return HandlerLaneContainerBudget().handle(
        ModelLaneContainerBudgetRequest(
            host=host,
            docker_ps=docker_ps,
            read_error=read_error,
            declarations=declarations,
        )
    )


def over_budget_fixture() -> str:
    return (FIXTURES / "docker_ps_over_budget.ndjson").read_text(encoding="utf-8")


def test_over_budget_fixture_fails_and_does_not_warn() -> None:
    verdict = judge(
        over_budget_fixture(),
        declaration("alpha", "proj-alpha", 2),
        declaration("beta", "proj-beta", 1),
    )

    by_lane = {lane.lane: lane for lane in verdict.lanes}
    assert by_lane["alpha"].status == "FAIL"
    assert by_lane["alpha"].observed == 3
    assert by_lane["alpha"].budget == 2
    assert by_lane["alpha"].containers == (
        "proj-alpha-agent-actions-1",
        "proj-alpha-runtime-1",
        "proj-alpha-runtime-effects-1",
    )
    assert by_lane["beta"].status == "PASS"
    assert verdict.status == "FAIL"
    assert verdict.failed is True
    assert "WARN" not in {verdict.status, *(lane.status for lane in verdict.lanes)}


def test_one_alarm_per_host_and_lane_upserts_on_rerun() -> None:
    handler = HandlerLaneContainerBudget()
    declarations = (
        declaration("alpha", "proj-alpha", 2),
        declaration("beta", "proj-beta", 1),
    )
    rows: tuple = ()
    for _ in range(2):
        verdict = judge(over_budget_fixture(), *declarations)
        assert [alarm.key for alarm in verdict.alarms] == [f"lane-budget:{HOST}:alpha"]
        rows = handler.upsert_alarms(rows, verdict)

    assert [row.key for row in rows] == [f"lane-budget:{HOST}:alpha"]
    assert rows[0].status == "FAIL"
    assert rows[0].observed == 3
    assert rows[0].budget == 2


def test_a_rerun_after_a_change_replaces_the_row_under_the_same_key() -> None:
    handler = HandlerLaneContainerBudget()
    over = judge(over_budget_fixture(), declaration("alpha", "proj-alpha", 2))
    worse = judge(over_budget_fixture(), declaration("alpha", "proj-alpha", 0))
    rows = handler.upsert_alarms(handler.upsert_alarms((), over), worse)

    assert len(rows) == 1
    assert rows[0].budget == 0


def test_within_budget_raises_no_alarm_and_does_not_fail() -> None:
    verdict = judge(over_budget_fixture(), declaration("alpha", "proj-alpha", 3))

    assert verdict.status == "PASS"
    assert verdict.failed is False
    assert verdict.alarms == ()


def test_only_running_application_containers_count() -> None:
    verdict = judge(over_budget_fixture(), declaration("alpha", "proj-alpha", 3))

    lane = verdict.lanes[0]
    # The exited container, the message bus, the database and the other lane's container are not counted.
    assert lane.observed == 3
    assert "proj-alpha-old-1" not in lane.containers
    assert "adhoc-test-db" not in lane.containers


def test_the_budget_is_the_declaration_not_a_constant() -> None:
    for budget, expected in ((0, "FAIL"), (2, "FAIL"), (3, "PASS"), (7, "PASS")):
        verdict = judge(
            over_budget_fixture(), declaration("alpha", "proj-alpha", budget)
        )
        assert verdict.lanes[0].status == expected, budget


def test_unknown_unreadable_host_fails_on_an_empty_fixture() -> None:
    verdict = judge("", declaration("alpha", "proj-alpha", 3))

    assert verdict.status == "UNKNOWN"
    assert verdict.failed is True
    assert verdict.lanes[0].status == "UNKNOWN"
    assert verdict.lanes[0].observed is None
    assert [alarm.key for alarm in verdict.alarms] == [f"lane-budget:{HOST}:alpha"]
    assert verdict.alarms[0].status == "UNKNOWN"


def test_unknown_when_the_read_reports_an_error_even_with_output() -> None:
    verdict = judge(
        over_budget_fixture(),
        declaration("alpha", "proj-alpha", 3),
        read_error="docker socket: permission denied",
    )

    assert verdict.status == "UNKNOWN"
    assert verdict.failed is True
    assert "permission denied" in verdict.alarms[0].detail


def test_unknown_when_a_listing_line_is_not_json() -> None:
    verdict = judge(
        over_budget_fixture() + "Cannot connect to the Docker daemon\n",
        declaration("alpha", "proj-alpha", 3),
    )

    assert verdict.status == "UNKNOWN"
    assert verdict.failed is True


def test_unknown_when_no_budget_is_declared_for_the_host() -> None:
    verdict = judge(
        over_budget_fixture(), declaration("alpha", "proj-alpha", 3), host="host-b"
    )

    assert verdict.status == "UNKNOWN"
    assert verdict.failed is True
    assert [alarm.key for alarm in verdict.alarms] == ["lane-budget:host-b:undeclared"]


def test_any_failing_lane_fails_the_host() -> None:
    verdict = judge(
        over_budget_fixture(),
        declaration("alpha", "proj-alpha", 1),
        declaration("beta", "proj-beta", 1),
    )
    assert verdict.status == "FAIL"


def test_label_values_with_commas_do_not_hide_the_compose_project() -> None:
    line = (
        '{"Names":"proj-alpha-runtime-1","State":"running","Labels":'
        '"com.docker.compose.project.config_files=/a.yml,/b.yml,'
        'com.docker.compose.project=proj-alpha"}\n'
    )
    verdict = judge(line, declaration("alpha", "proj-alpha", 0))

    assert verdict.lanes[0].observed == 1
    assert verdict.lanes[0].status == "FAIL"


def test_contract_names_the_shared_models_and_handler() -> None:
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))

    assert raw["name"] == "node_lane_container_budget_compute"
    assert raw["handler"]["class"] == "HandlerLaneContainerBudget"
    assert (
        raw["input_model"]["module"] == "omnimarket.models.model_lane_container_budget"
    )
    assert raw["event_bus"]["publish_topics"] == []
