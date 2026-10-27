"""The controller repair plan: target selection, the bounded brief and the lane liveness read (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from omnimarket.nodes.node_controller_fix_plan_compute.handler import (
    HandlerControllerFixPlan,
)
from omnimarket.nodes.node_controller_fix_plan_compute.models import (
    ModelControllerFixPlanRequest,
)

NOW = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
CONTRACT = {
    "controller_fix_model": "claude",
    "controller_fix_effort": "xhigh",
    "controller_fix_route": "route model=claude dest=claude-lane effort=xhigh",
    "controller_fix_brief_prs": 2,
    "ticket": "OMN-TEST",
    "ruling": "RULING test",
}


def host(name: str, claude: int, codex: int) -> dict:
    return {"name": name, "admission": {"claude_slots": claude, "codex_slots": codex}}


def plan(**fields):
    return HandlerControllerFixPlan().handle(ModelControllerFixPlanRequest(**fields))


def test_the_controller_host_is_preferred_when_admitted() -> None:
    pool = {
        "live_admission": True,
        "hosts": [host("host-a", 1, 1), host("host-b", 5, 1)],
    }
    result = plan(
        operation="target",
        contract=CONTRACT,
        controller_host="host-a",
        lab_headroom=pool,
    )
    assert result.host == "host-a"
    assert result.error == ""
    assert result.contract["controller_fix_model"] == "codex"


def test_an_exhausted_claude_pool_falls_through_to_codex() -> None:
    pool = {"live_admission": True, "hosts": [host("host-a", 0, 2)]}
    result = plan(
        operation="target",
        contract=CONTRACT,
        controller_host="host-a",
        lab_headroom=pool,
    )
    assert result.host == "host-a"
    assert result.contract["controller_fix_model"] == "codex"
    assert "model=codex" in result.contract["controller_fix_route"]


def test_no_admitted_host_is_an_error_not_a_host() -> None:
    pool = {"live_admission": True, "hosts": [host("host-a", 0, 0)]}
    result = plan(operation="target", contract=CONTRACT, lab_headroom=pool)
    assert result.host is None
    assert "no admitted Claude or Codex host" in result.error


def test_an_absent_pool_read_keeps_the_runner_preflight() -> None:
    result = plan(
        operation="target",
        contract=CONTRACT,
        controller_host="host-a",
        lab_headroom=None,
    )
    assert result.host == "host-a"
    assert result.error == ""
    assert result.contract == {
        **CONTRACT,
        "controller_fix_model": "sonnet",
        "controller_fix_effort": "high",
        "controller_fix_route": "route model=sonnet dest=sonnet-lane effort=high",
    }


def test_the_brief_names_the_bound_and_caps_the_listed_prs() -> None:
    defects = [
        {
            "key": f"repo-a#{n}",
            "head": "abc",
            "age": 61,
            "merge_state": "clean",
            "armed": False,
            "title": "t",
        }
        for n in range(3)
    ]
    brief = plan(
        operation="brief",
        contract=CONTRACT,
        defects=defects,
        lane="lane-test",
        bound_min=30,
    ).brief
    assert "# Lane lane-test" in brief
    assert "3 PRs green on every check" in brief
    assert "(+1 more green heads" in brief
    assert "repo-a#0 head abc" in brief
    assert "repo-a#2 head" not in brief


def test_a_lane_that_ended_without_running_its_brief_is_dead_and_its_heads_are_resent() -> (
    None
):
    started = (NOW - timedelta(minutes=10)).isoformat()
    result = plan(
        operation="lanes",
        contract={},
        now=NOW.isoformat(),
        live_min=90,
        dispatched={"repo-a#1@abc": {"lane": "lane-old", "at": started}},
        idents={"repo-a#1": "repo-a#1@abc"},
        receipts={"lane-old": {"started": started, "final": True, "status": "no-host"}},
    )
    assert result.dead == ["lane-old"]
    assert result.resend == ["repo-a#1"]
    assert result.live is None


def test_the_newest_unended_lane_inside_the_window_is_live() -> None:
    result = plan(
        operation="lanes",
        contract={},
        now=NOW.isoformat(),
        live_min=90,
        receipts={
            "lane-a": {"started": (NOW - timedelta(minutes=80)).isoformat()},
            "lane-b": {"started": (NOW - timedelta(minutes=20)).isoformat()},
            "lane-c": {"started": (NOW - timedelta(minutes=200)).isoformat()},
        },
    )
    assert result.live == "lane-b"


def test_the_node_refuses_without_a_registry_root_or_onex_bin() -> None:
    from omnimarket.nodes.node_controller_fix_plan_compute import node

    result = node.controller_fix_target(
        {"lab_headroom": None}, CONTRACT, "host-a", environ={}
    )
    assert result[1] is None
    assert "set ONEX_REGISTRY_ROOT or ONEX_BIN" in result[2]
