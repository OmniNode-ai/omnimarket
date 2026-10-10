# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing class plan compute: which stale-summary and retarget actions a tick performs.

The cases below are the ones the live controller's own ``class_plan`` was run on beside this node (the
same candidates, acted-on subjects, quota cap, memories and clock); the expected values are what it
returned, with its plan tuples, deferred PRs, next sidecar memories and ``landing_classes`` summary.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pr_landing_class_plan_compute import (
    HandlerPrLandingClassPlan,
    NodePrLandingClassPlanCompute,
    plan_landing_classes,
)
from omnimarket.nodes.node_pr_landing_class_plan_compute.handlers import (
    handler_pr_landing_class_plan,
)
from omnimarket.nodes.node_pr_landing_class_plan_compute.models.model_landing_class_plan import (
    ModelLandingClassPlanRequest,
    ModelLandingClassPlanResult,
)

NODE_DIR = Path(handler_pr_landing_class_plan.__file__).resolve().parents[1]

CASES: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
    (
        "all_fit",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "pending_gated": [
                {"names": ["Tests (Split 1/20)", "lint"], "short": "omnimarket#11"}
            ],
            "retarget": [
                {"head": "h3", "short": "omniclaude#3", "step": "retarget"},
                {"head": "h4", "short": "omnidash#4", "step": "comment"},
                {"head": "h5", "short": "omnidash#5", "step": "wait"},
            ],
            "stale": [
                {"head": "h10", "short": "omnimarket#10", "step": "rerun"},
                {"head": "h7", "short": "omnibase_core#7", "step": "update_branch"},
            ],
        },
        {
            "deferred": [],
            "plan": [
                ["omnibase_core#7", "update_branch", "h7"],
                ["omnimarket#10", "rerun", "h10"],
                ["omniclaude#3", "retarget", "h3"],
                ["omnidash#4", "comment", "h4"],
            ],
            "retarget_mem": {
                "omniclaude#3": {"at": "2026-10-09T12:00:00Z", "head": "h3", "n": 1},
                "omnidash#4": {"at": "2026-10-09T12:00:00Z", "commented": True},
            },
            "stale_mem": {
                "omnibase_core#7": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h7": ["update_branch"]},
                    "refreshes": 1,
                },
                "omnimarket#10": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h10": ["rerun"]},
                    "refreshes": 0,
                },
            },
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {"omnimarket#11": ["Tests (Split 1/20)", "lint"]},
                "reads": 2,
                "retarget": ["omniclaude#3"],
                "retarget_ask": ["omnidash#4"],
                "retarget_wait": ["omnidash#5"],
                "stale_copy_rerun": [],
                "stale_refresh": ["omnibase_core#7"],
                "stale_rerun": ["omnimarket#10"],
            },
        },
    ),
    (
        "cap_defers",
        {
            "class_cap": 5,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [
                {"head": "h3", "short": "c#3", "step": "retarget"},
                {"head": "h4", "short": "d#4", "step": "comment"},
            ],
            "stale": [
                {"head": "h1", "short": "a#1", "step": "rerun"},
                {"head": "h2", "short": "b#2", "step": "update_branch"},
            ],
        },
        {
            "deferred": [["c#3", "retarget", "h3"], ["d#4", "comment", "h4"]],
            "plan": [["a#1", "rerun", "h1"], ["b#2", "update_branch", "h2"]],
            "retarget_mem": {},
            "stale_mem": {
                "a#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h1": ["rerun"]},
                    "refreshes": 0,
                },
                "b#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h2": ["update_branch"]},
                    "refreshes": 1,
                },
            },
            "summary": {
                "cap": 5,
                "deferred": ["c#3", "d#4"],
                "pending_gated": {},
                "reads": 2,
                "retarget": [],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": ["b#2"],
                "stale_rerun": ["a#1"],
            },
        },
    ),
    (
        "small_after_big_still_fits",
        {
            "class_cap": 4,
            "class_reads": 1,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [
                {"head": "h2", "short": "b#2", "step": "retarget"},
                {"head": "h3", "short": "c#3", "step": "comment"},
            ],
            "stale": [{"head": "h1", "short": "a#1", "step": "rerun"}],
        },
        {
            "deferred": [["b#2", "retarget", "h2"]],
            "plan": [["a#1", "rerun", "h1"], ["c#3", "comment", "h3"]],
            "retarget_mem": {"c#3": {"at": "2026-10-09T12:00:00Z", "commented": True}},
            "stale_mem": {
                "a#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h1": ["rerun"]},
                    "refreshes": 0,
                }
            },
            "summary": {
                "cap": 4,
                "deferred": ["b#2"],
                "pending_gated": {},
                "reads": 1,
                "retarget": [],
                "retarget_ask": ["c#3"],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": ["a#1"],
            },
        },
    ),
    (
        "reads_over_cap",
        {
            "class_cap": 2,
            "class_reads": 4,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [{"head": "h2", "short": "b#2", "step": "comment"}],
            "stale": [{"head": "h1", "short": "a#1", "step": "update_branch"}],
        },
        {
            "deferred": [["a#1", "update_branch", "h1"], ["b#2", "comment", "h2"]],
            "plan": [],
            "retarget_mem": {},
            "stale_mem": {},
            "summary": {
                "cap": 2,
                "deferred": ["a#1", "b#2"],
                "pending_gated": {},
                "reads": 4,
                "retarget": [],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": [],
            },
        },
    ),
    (
        "m4_first",
        {
            "class_cap": 5,
            "class_reads": 2,
            "m4": ["z#9"],
            "now": "2026-10-09T12:00:00Z",
            "retarget": [{"head": "h2", "short": "b#2", "step": "comment"}],
            "stale": [
                {"head": "h1", "short": "a#1", "step": "rerun"},
                {"head": "h9", "short": "z#9", "step": "rerun"},
            ],
        },
        {
            "deferred": [["a#1", "rerun", "h1"]],
            "plan": [["z#9", "rerun", "h9"], ["b#2", "comment", "h2"]],
            "retarget_mem": {"b#2": {"at": "2026-10-09T12:00:00Z", "commented": True}},
            "stale_mem": {
                "z#9": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h9": ["rerun"]},
                    "refreshes": 0,
                }
            },
            "summary": {
                "cap": 5,
                "deferred": ["a#1"],
                "pending_gated": {},
                "reads": 2,
                "retarget": [],
                "retarget_ask": ["b#2"],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": ["z#9"],
            },
        },
    ),
    (
        "acted_skipped",
        {
            "acted": ["OmniNode-ai/a#1", "kill_worker:lease-3", "OmniNode-ai/c#3"],
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [
                {"head": "h3", "short": "c#3", "step": "wait"},
                {"head": "h4", "short": "d#4", "step": "retarget"},
            ],
            "stale": [
                {"head": "h1", "short": "a#1", "step": "rerun"},
                {"head": "h2", "short": "b#2", "step": "update_branch"},
            ],
        },
        {
            "deferred": [],
            "plan": [["b#2", "update_branch", "h2"], ["d#4", "retarget", "h4"]],
            "retarget_mem": {
                "d#4": {"at": "2026-10-09T12:00:00Z", "head": "h4", "n": 1}
            },
            "stale_mem": {
                "b#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h2": ["update_branch"]},
                    "refreshes": 1,
                }
            },
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {},
                "reads": 2,
                "retarget": ["d#4"],
                "retarget_ask": [],
                "retarget_wait": ["c#3"],
                "stale_copy_rerun": [],
                "stale_refresh": ["b#2"],
                "stale_rerun": [],
            },
        },
    ),
    (
        "copy_rerun_memo",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "stale": [
                {"head": "h1", "run_id": "123456", "short": "a#1", "step": "rerun_run"}
            ],
            "stale_memory": {
                "a#1": {
                    "at": "2026-10-09T11:00:00Z",
                    "heads": {"h1": ["rerun_run:99"]},
                    "refreshes": 1,
                }
            },
        },
        {
            "deferred": [],
            "plan": [["a#1", "rerun_run", "h1"]],
            "retarget_mem": {},
            "stale_mem": {
                "a#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h1": ["rerun_run:99", "rerun_run:123456"]},
                    "refreshes": 1,
                }
            },
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {},
                "reads": 2,
                "retarget": [],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": ["a#1"],
                "stale_refresh": [],
                "stale_rerun": [],
            },
        },
    ),
    (
        "memory_next",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "pr_states": {"a#1": "OPEN", "d#4": "closed", "gone#1": "MERGED"},
            "retarget": [
                {"head": "h3", "short": "c#3", "step": "retarget"},
                {"head": "h4", "short": "d#4", "step": "retarget"},
                {"head": "h5", "short": "e#5", "step": "comment"},
            ],
            "retarget_memory": {
                "c#3": {"at": "2026-10-09T10:00:00Z", "head": "h3", "n": 1},
                "d#4": {"at": "2026-10-09T10:00:00Z", "head": "old", "n": 2},
                "e#5": {"at": "2026-10-09T10:00:00Z", "head": "h5", "n": 1},
            },
            "stale": [
                {"head": "h1", "short": "a#1", "step": "update_branch"},
                {"head": "h2", "short": "b#2", "step": "rerun"},
            ],
            "stale_memory": {
                "a#1": {
                    "at": "2026-10-09T10:00:00Z",
                    "heads": {"h0": ["rerun"]},
                    "refreshes": 1,
                },
                "gone#1": {
                    "at": "2026-10-09T10:00:00Z",
                    "heads": {"x": ["rerun"]},
                    "refreshes": 1,
                },
            },
        },
        {
            "deferred": [["d#4", "retarget", "h4"]],
            "plan": [
                ["a#1", "update_branch", "h1"],
                ["b#2", "rerun", "h2"],
                ["c#3", "retarget", "h3"],
                ["e#5", "comment", "h5"],
            ],
            "retarget_mem": {
                "c#3": {"at": "2026-10-09T12:00:00Z", "head": "h3", "n": 2},
                "e#5": {
                    "at": "2026-10-09T12:00:00Z",
                    "commented": True,
                    "head": "h5",
                    "n": 1,
                },
            },
            "stale_mem": {
                "a#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h1": ["update_branch"]},
                    "refreshes": 2,
                },
                "b#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h2": ["rerun"]},
                    "refreshes": 0,
                },
            },
            "summary": {
                "cap": 8,
                "deferred": ["d#4"],
                "pending_gated": {},
                "reads": 2,
                "retarget": ["c#3"],
                "retarget_ask": ["e#5"],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": ["a#1"],
                "stale_rerun": ["b#2"],
            },
        },
    ),
    (
        "terminal_memory_dropped_unknown_kept",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "pr_states": {"c#2": "Closed", "m#1": "MERGED", "o#4": "OPEN"},
            "retarget_memory": {
                "m#1": {"at": "t", "head": "x", "n": 1},
                "u#3": {"at": "t", "commented": True},
            },
            "stale_memory": {
                "c#2": {"at": "t", "heads": {}, "refreshes": 2},
                "m#1": {"at": "t", "heads": {"x": ["rerun"]}, "refreshes": 0},
                "u#3": {"at": "t", "heads": {}, "refreshes": 0},
            },
        },
        {
            "deferred": [],
            "plan": [],
            "retarget_mem": {"u#3": {"at": "t", "commented": True}},
            "stale_mem": {"u#3": {"at": "t", "heads": {}, "refreshes": 0}},
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {},
                "reads": 2,
                "retarget": [],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": [],
            },
        },
    ),
    (
        "empty",
        {"class_cap": 0, "class_reads": 0, "now": "2026-10-09T12:00:00Z"},
        {
            "deferred": [],
            "plan": [],
            "retarget_mem": {},
            "stale_mem": {},
            "summary": {
                "cap": 0,
                "deferred": [],
                "pending_gated": {},
                "reads": 0,
                "retarget": [],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": [],
            },
        },
    ),
    (
        "same_pr_both",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [{"head": "h1", "short": "a#1", "step": "retarget"}],
            "stale": [{"head": "h1", "short": "a#1", "step": "rerun"}],
        },
        {
            "deferred": [],
            "plan": [["a#1", "rerun", "h1"], ["a#1", "retarget", "h1"]],
            "retarget_mem": {
                "a#1": {"at": "2026-10-09T12:00:00Z", "head": "h1", "n": 1}
            },
            "stale_mem": {
                "a#1": {
                    "at": "2026-10-09T12:00:00Z",
                    "heads": {"h1": ["rerun"]},
                    "refreshes": 0,
                }
            },
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {},
                "reads": 2,
                "retarget": ["a#1"],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": ["a#1"],
            },
        },
    ),
    (
        "retarget_counts_per_head",
        {
            "class_cap": 8,
            "class_reads": 2,
            "now": "2026-10-09T12:00:00Z",
            "retarget": [
                {"head": "new", "short": "a#1", "step": "retarget"},
                {"head": "same", "short": "b#2", "step": "retarget"},
            ],
            "retarget_memory": {
                "a#1": {"at": "2026-10-09T10:00:00Z", "head": "old", "n": 2},
                "b#2": {
                    "at": "2026-10-09T10:00:00Z",
                    "commented": True,
                    "head": "same",
                    "n": 1,
                },
            },
        },
        {
            "deferred": [],
            "plan": [["a#1", "retarget", "new"], ["b#2", "retarget", "same"]],
            "retarget_mem": {
                "a#1": {"at": "2026-10-09T12:00:00Z", "head": "new", "n": 1},
                "b#2": {
                    "at": "2026-10-09T12:00:00Z",
                    "commented": True,
                    "head": "same",
                    "n": 2,
                },
            },
            "stale_mem": {},
            "summary": {
                "cap": 8,
                "deferred": [],
                "pending_gated": {},
                "reads": 2,
                "retarget": ["a#1", "b#2"],
                "retarget_ask": [],
                "retarget_wait": [],
                "stale_copy_rerun": [],
                "stale_refresh": [],
                "stale_rerun": [],
            },
        },
    ),
]

CASES_BY_NAME = {c[0]: (c[1], c[2]) for c in CASES}


def _request(raw: dict[str, Any]) -> ModelLandingClassPlanRequest:
    return ModelLandingClassPlanRequest.model_validate(
        {**raw, "now": raw["now"].replace("Z", "+00:00")}
    )


def _live_shape(result: ModelLandingClassPlanResult) -> dict[str, Any]:
    """The result in the shape the live controller's class_plan returned."""
    plan = [[a.short, a.step, a.head] for a in result.plan]
    deferred = [[a.short, a.step, a.head] for a in result.deferred]
    stale_mem = {
        k: {"heads": {h: list(s) for h, s in v.heads.items()}, "refreshes": v.refreshes}
        | ({"at": v.at} if v.at else {})
        for k, v in result.stale_memory.items()
    }
    retarget_mem = {
        k: {
            name: val
            for name, val in v.model_dump().items()
            if val not in (None, 0, False)
        }
        for k, v in result.retarget_memory.items()
    }
    summary = result.summary.model_dump(mode="json")
    summary["pending_gated"] = {k: list(v) for k, v in summary["pending_gated"].items()}
    for key, val in summary.items():
        if isinstance(val, tuple):
            summary[key] = list(val)
    return {
        "plan": plan,
        "deferred": deferred,
        "stale_mem": stale_mem,
        "retarget_mem": retarget_mem,
        "summary": summary,
    }


def _plan(raw: dict[str, Any]) -> dict[str, Any]:
    return _live_shape(HandlerPrLandingClassPlan().handle(_request(raw)))


@pytest.mark.unit
def test_contract_declares_a_pure_compute_with_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["name"] == "node_pr_landing_class_plan_compute"
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["descriptor"]["purity"] == "pure"
    for side, model in (
        ("input_model", ModelLandingClassPlanRequest),
        ("output_model", ModelLandingClassPlanResult),
    ):
        declared = contract[side]
        module = importlib.import_module(declared["module"])
        assert getattr(module, declared["name"]) is model
    handler = contract["handler"]
    assert (
        getattr(importlib.import_module(handler["module"]), handler["class"])
        is HandlerPrLandingClassPlan
    )
    dispatch = contract["runtime_dispatch"]
    assert dispatch["command_topic"].startswith("onex.cmd.omnimarket.")
    assert set(dispatch["terminal_events"]) == {"success", "failure"}


@pytest.mark.unit
def test_handler_is_definition_b() -> None:
    params = list(
        inspect.signature(HandlerPrLandingClassPlan.handle).parameters.values()
    )
    assert [p.name for p in params] == ["self", "request"]
    assert not inspect.iscoroutinefunction(HandlerPrLandingClassPlan.handle)
    assert issubclass(NodePrLandingClassPlanCompute, HandlerPrLandingClassPlan)
    request = _request(
        {"now": "2026-10-09T12:00:00Z", "class_cap": 4, "class_reads": 0}
    )
    assert isinstance(
        HandlerPrLandingClassPlan().handle(request), ModelLandingClassPlanResult
    )
    assert plan_landing_classes(request) == HandlerPrLandingClassPlan().handle(request)


@pytest.mark.unit
def test_handler_does_no_io() -> None:
    tree = ast.parse(Path(handler_pr_landing_class_plan.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"os", "subprocess", "socket", "time", "random", "pathlib"}


@pytest.mark.unit
@pytest.mark.parametrize(("name", "raw", "expected"), CASES, ids=[c[0] for c in CASES])
def test_plan_equals_the_live_controllers(
    name: str, raw: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert _plan(raw) == expected


@pytest.mark.unit
def test_the_cap_goes_to_cheap_actions_after_a_dear_one_is_deferred() -> None:
    plan = _plan(dict(CASES_BY_NAME["small_after_big_still_fits"][0]))
    assert [p[0] for p in plan["plan"]] == ["a#1", "c#3"]
    assert [p[0] for p in plan["deferred"]] == ["b#2"]


@pytest.mark.unit
def test_a_pr_the_decision_acts_on_gets_no_class_action_but_is_still_reported_waiting() -> (
    None
):
    plan = _plan(dict(CASES_BY_NAME["acted_skipped"][0]))
    assert "a#1" not in [p[0] for p in plan["plan"]]
    assert plan["summary"]["retarget_wait"] == ["c#3"]


@pytest.mark.unit
def test_the_m4_class_takes_the_cap_first() -> None:
    plan = _plan(dict(CASES_BY_NAME["m4_first"][0]))
    assert plan["plan"][0][0] == "z#9"
    assert [p[0] for p in plan["deferred"]] == ["a#1"]


@pytest.mark.unit
def test_memory_of_a_merged_or_closed_pr_is_dropped_and_an_unknown_one_kept() -> None:
    plan = _plan(dict(CASES_BY_NAME["terminal_memory_dropped_unknown_kept"][0]))
    assert set(plan["stale_mem"]) == {"u#3"}
    assert set(plan["retarget_mem"]) == {"u#3"}


@pytest.mark.unit
def test_the_same_candidates_in_any_order_give_the_same_plan() -> None:
    raw = dict(CASES_BY_NAME["memory_next"][0])
    reference = _plan(raw)
    rng = random.Random(20672)
    for _ in range(8):
        shuffled = {
            k: (rng.sample(v, len(v)) if isinstance(v, list) else v)
            for k, v in raw.items()
        }
        assert _plan(shuffled) == reference


@pytest.mark.unit
def test_the_request_is_validated() -> None:
    ok = {"now": "2026-10-09T12:00:00+00:00", "class_cap": 4, "class_reads": 0}
    ModelLandingClassPlanRequest.model_validate(ok)
    with pytest.raises(ValidationError):  # a naive clock
        ModelLandingClassPlanRequest.model_validate(
            {**ok, "now": "2026-10-09T12:00:00"}
        )
    with pytest.raises(ValidationError):  # a PR named twice
        ModelLandingClassPlanRequest.model_validate(
            {
                **ok,
                "stale": [
                    {"short": "a#1", "step": "rerun", "head": "h"},
                    {"short": "a#1", "step": "update_branch", "head": "h"},
                ],
            }
        )
    with pytest.raises(ValidationError):  # a copy rerun without its run
        ModelLandingClassPlanRequest.model_validate(
            {**ok, "stale": [{"short": "a#1", "step": "rerun_run", "head": "h"}]}
        )
    with pytest.raises(ValidationError):  # a run on a step that reruns no run
        ModelLandingClassPlanRequest.model_validate(
            {
                **ok,
                "stale": [
                    {"short": "a#1", "step": "rerun", "head": "h", "run_id": "7"}
                ],
            }
        )
    with pytest.raises(ValidationError):  # a step the controller has no cost for
        ModelLandingClassPlanRequest.model_validate(
            {**ok, "retarget": [{"short": "a#1", "step": "close", "head": "h"}]}
        )
    with pytest.raises(ValidationError):  # a negative cap
        ModelLandingClassPlanRequest.model_validate({**ok, "class_cap": -1})


@pytest.mark.unit
def test_the_clock_is_read_in_utc() -> None:
    raw = dict(CASES_BY_NAME["all_fit"][0])
    plus_two = {**raw, "now": "2026-10-09T14:00:00+02:00"}
    request = ModelLandingClassPlanRequest.model_validate(plus_two)
    assert request.now == datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
    result = HandlerPrLandingClassPlan().handle(request)
    assert {m.at for m in result.stale_memory.values()} == {"2026-10-09T12:00:00Z"}
