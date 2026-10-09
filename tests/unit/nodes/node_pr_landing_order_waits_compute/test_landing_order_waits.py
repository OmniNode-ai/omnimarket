# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing order waits compute: which PRs a lane parked behind another PR, and which waits fired.

The expected values are what the live controller's own wait read (``drain_map.lane_wait`` and
``wait_trigger`` as ``landing_ledger`` calls them) returned for the same rows, clock and heads.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.nodes.node_pr_landing_order_waits_compute import (
    HandlerPrLandingOrderWaits,
    NodePrLandingOrderWaitsCompute,
    derive_order_waits,
)
from omnimarket.nodes.node_pr_landing_order_waits_compute.handlers import (
    handler_pr_landing_order_waits,
)
from omnimarket.nodes.node_pr_landing_order_waits_compute.models.model_landing_order_waits import (
    ModelLandingOrderWaitPr,
    ModelLandingOrderWaitsRequest,
    ModelLandingOrderWaitsResult,
)

NODE_DIR = Path(handler_pr_landing_order_waits.__file__).resolve().parents[1]
NOW = datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC)
HEAD = "aaaaaaaaaaaa1111111111111111111111111111"
OTHER_HEAD = "bbbbbbbbbbbb2222222222222222222222222222"


def row(ts: str, rtype: str, lane: str, body: str) -> ModelLandingLedgerRow:
    return ModelLandingLedgerRow(
        ts=ts, rtype=rtype, lane=lane, text=f"{ts} | {rtype} | lane={lane} | {body}"
    )


def waiting(
    ts: str,
    pr: str,
    extra: str = "",
    lane: str = "land-a",
    outcome: str = "waiting_order",
) -> ModelLandingLedgerRow:
    return row(ts, "TERMINAL", lane, f"outcome={outcome} | pr={pr} | {extra}")


def run(
    rows: list[ModelLandingLedgerRow],
    prs: dict[str, str],
    states: dict[str, Any] | None = None,
    now: datetime = NOW,
) -> ModelLandingOrderWaitsResult:
    return HandlerPrLandingOrderWaits().handle(
        ModelLandingOrderWaitsRequest(
            now=now,
            rows=tuple(rows),
            prs=tuple(
                ModelLandingOrderWaitPr(key=k, head_sha=h) for k, h in prs.items()
            ),
            predecessor_states=states or {},
        )
    )


def test_open_predecessor_keeps_the_wait_open_with_its_source_named() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | parents=omnibase_infra#4686,omnimarket#3300",
            )
        ],
        {"omnimarket#3301": HEAD},
        {"omnibase_infra#4686": "open", "omnimarket#3300": "merged"},
    )
    assert result.waits_fired == {}
    wait = result.waits_open["omnimarket#3301"]
    assert wait.parents == ("omnibase_infra#4686", "omnimarket#3300")
    assert wait.state == "waits on omnibase_infra#4686 (open; from parents=)"
    assert wait.fired == ""
    assert wait.head == "aaaaaaaaaaaa"
    assert wait.lane == "land-a"
    assert wait.ts == "2026-10-09T09:00:00Z"


def test_unknown_predecessor_keeps_the_wait_and_says_unknown() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | waits=infra#4686",
            )
        ],
        {"omnimarket#3301": HEAD},
    )
    wait = result.waits_open["omnimarket#3301"]
    assert wait.parents == ("infra#4686",)
    assert wait.state == "waits on infra#4686 (unknown; from waits=)"


def test_open_and_unknown_together_read_unknown() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | parents=a#1,b#2",
            )
        ],
        {"omnimarket#3301": HEAD},
        {"a#1": "open"},
    )
    assert (
        result.waits_open["omnimarket#3301"].state
        == "waits on a#1, b#2 (unknown; from parents=)"
    )


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_wait_fires_when_every_predecessor_is_merged_or_closed(state: str) -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | parents=a#1,b#2",
            )
        ],
        {"omnimarket#3301": HEAD},
        {"a#1": "merged", "b#2": state},
    )
    assert result.waits_open == {}
    wait = result.waits_fired["omnimarket#3301"]
    assert wait.fired == "predecessor(s) a#1, b#2 no longer open"
    assert wait.state == ""


def test_prose_predecessor_and_the_pr_itself_are_filtered() -> None:
    result = run(
        [
            row(
                "2026-10-09T09:00:00Z",
                "TERMINAL",
                "land-omnimarket-3301-x",
                "outcome=waiting_order | pr=omnimarket#3301 head=aaaaaaaaaaaa stacked on "
                "OmniNode-ai/omnimarket#3300 and omnimarket#3301",
            )
        ],
        {"omnimarket#3301": HEAD},
        {"omnimarket#3300": "open"},
    )
    wait = result.waits_open["omnimarket#3301"]
    assert wait.parents == ("omnimarket#3300",)
    assert wait.state == "waits on omnimarket#3300 (open; from prose)"


def test_a_field_naming_the_pr_itself_drops_it_from_the_predecessors() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | parents=omnimarket#3301,OmniNode-ai/omnimarket#3300,omnimarket#3300",
            )
        ],
        {"omnimarket#3301": HEAD},
        {"omnimarket#3300": "open"},
    )
    assert result.waits_open["omnimarket#3301"].parents == ("omnimarket#3300",)


def test_a_parent_field_without_pr_tokens_names_no_predecessor() -> None:
    rows = [
        waiting(
            "2026-10-09T09:30:00Z",
            "omnimarket#3303",
            "head=aaaaaaaaaaaa | parent=repo-drains-2a21b",
        )
    ]
    fresh = run(rows, {"omnimarket#3303": HEAD})
    assert fresh.waits_open["omnimarket#3303"].state == "no predecessor named"
    assert fresh.waits_open["omnimarket#3303"].parents == ()
    aged = run(
        rows,
        {"omnimarket#3303": HEAD},
        now=datetime(2026, 10, 9, 11, 30, 1, tzinfo=UTC),
    )
    assert aged.waits_open == {}
    assert (
        aged.waits_fired["omnimarket#3303"].fired
        == "no predecessor named and 2h passed"
    )


def test_a_wait_naming_no_predecessor_fires_at_exactly_two_hours() -> None:
    rows = [waiting("2026-10-09T09:00:00Z", "omnimarket#3303", "head=aaaaaaaaaaaa")]
    before = run(
        rows,
        {"omnimarket#3303": HEAD},
        now=datetime(2026, 10, 9, 10, 59, 59, tzinfo=UTC),
    )
    assert before.waits_open["omnimarket#3303"].state == "no predecessor named"
    at = run(
        rows, {"omnimarket#3303": HEAD}, now=datetime(2026, 10, 9, 11, 0, 0, tzinfo=UTC)
    )
    assert (
        at.waits_fired["omnimarket#3303"].fired == "no predecessor named and 2h passed"
    )


def test_wait_without_head_counts_six_hours() -> None:
    rows = [waiting("2026-10-09T04:00:00Z", "omnimarket#3301", "parents=a#1")]
    assert run(
        rows,
        {"omnimarket#3301": HEAD},
        {"a#1": "open"},
        now=datetime(2026, 10, 9, 10, 0, 0, tzinfo=UTC),
    ).waits_open
    stale = run(
        rows,
        {"omnimarket#3301": HEAD},
        {"a#1": "open"},
        now=datetime(2026, 10, 9, 10, 0, 1, tzinfo=UTC),
    )
    assert stale.waits_open == {}
    assert stale.waits_fired == {}


def test_wait_without_head_reports_the_pr_head() -> None:
    result = run(
        [waiting("2026-10-09T09:00:00Z", "omnimarket#3301", "parents=a#1")],
        {"omnimarket#3301": HEAD},
        {"a#1": "open"},
    )
    assert result.waits_open["omnimarket#3301"].head == HEAD


def test_a_new_head_ends_the_wait_and_a_prefix_of_the_head_keeps_it() -> None:
    rows = [
        waiting(
            "2026-10-09T09:00:00Z", "omnimarket#3301", "head=aaaaaaaaaaaa | parents=a#1"
        )
    ]
    states = {"a#1": "open"}
    assert run(rows, {"omnimarket#3301": OTHER_HEAD}, states).waits_open == {}
    assert "omnimarket#3301" in run(rows, {"omnimarket#3301": HEAD}, states).waits_open
    assert (
        "omnimarket#3301"
        in run(rows, {"omnimarket#3301": "aaaaaaa"}, states).waits_open
    )
    assert "omnimarket#3301" in run(rows, {"omnimarket#3301": ""}, states).waits_open


def test_ci_and_lab_waits_are_not_order_waits() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa",
                outcome="waiting_ci",
            ),
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3302",
                "head=aaaaaaaaaaaa",
                outcome="no_host",
            ),
        ],
        {"omnimarket#3301": HEAD, "omnimarket#3302": HEAD},
    )
    assert result.waits_open == {}
    assert result.waits_fired == {}


def test_the_newest_terminal_about_the_pr_decides() -> None:
    parked = waiting(
        "2026-10-09T08:00:00Z", "omnimarket#3301", "head=aaaaaaaaaaaa | parents=a#1"
    )
    later = waiting(
        "2026-10-09T09:00:00Z",
        "omnimarket#3301",
        "head=aaaaaaaaaaaa",
        outcome="done",
        lane="land-b",
    )
    result = run([parked, later], {"omnimarket#3301": HEAD}, {"a#1": "open"})
    assert result.waits_open == {}
    same_stamp = waiting(
        "2026-10-09T08:00:00Z",
        "omnimarket#3301",
        "head=aaaaaaaaaaaa",
        outcome="done",
        lane="land-b",
    )
    assert (
        run([parked, same_stamp], {"omnimarket#3301": HEAD}, {"a#1": "open"}).waits_open
        == {}
    )
    assert run(
        [same_stamp, parked], {"omnimarket#3301": HEAD}, {"a#1": "open"}
    ).waits_open


def test_a_terminal_older_than_the_window_is_not_read() -> None:
    old = waiting(
        "2026-10-02T09:59:59Z", "omnimarket#3301", "head=aaaaaaaaaaaa | parents=a#1"
    )
    assert run([old], {"omnimarket#3301": HEAD}, {"a#1": "open"}).waits_open == {}
    edge = waiting(
        "2026-10-02T10:00:00Z", "omnimarket#3301", "head=aaaaaaaaaaaa | parents=a#1"
    )
    assert run([edge], {"omnimarket#3301": HEAD}, {"a#1": "open"}).waits_open


def test_a_terminal_only_citing_the_pr_in_prose_is_not_about_it() -> None:
    roll_up = row(
        "2026-10-09T09:00:00Z",
        "TERMINAL",
        "land-z",
        "outcome=waiting_order | pr=omnimarket#3309 | head=aaaaaaaaaaaa | parents=a#1 | also omnimarket#3301",
    )
    assert run([roll_up], {"omnimarket#3301": HEAD}, {"a#1": "open"}).waits_open == {}


def test_the_pr_field_matches_a_repo_alias_and_a_bare_number_with_repo_field() -> None:
    alias = row(
        "2026-10-09T09:00:00Z",
        "TERMINAL",
        "land-z",
        "outcome=waiting_order | pr=market#3301 | head=aaaaaaaaaaaa | parents=a#1",
    )
    bare = row(
        "2026-10-09T09:00:00Z",
        "TERMINAL",
        "land-y",
        "outcome=waiting_order | repo=omnimarket | pr=3302 | head=aaaaaaaaaaaa | parents=a#1",
    )
    result = run(
        [alias, bare],
        {"omnimarket#3301": HEAD, "omnimarket#3302": HEAD},
        {"a#1": "open"},
    )
    assert set(result.waits_open) == {"omnimarket#3301", "omnimarket#3302"}


def test_a_per_pr_lane_without_a_pr_field_is_about_its_pr() -> None:
    own = row(
        "2026-10-09T09:00:00Z",
        "TERMINAL",
        "land-omnimarket-3301-a1",
        "outcome=waiting_order | head=aaaaaaaaaaaa | parents=a#1 | omnimarket#3301 is parked",
    )
    other = row(
        "2026-10-09T09:00:00Z",
        "TERMINAL",
        "drain-x",
        "outcome=waiting_order | head=aaaaaaaaaaaa | parents=a#1 | omnimarket#3302 is parked",
    )
    result = run(
        [own, other],
        {"omnimarket#3301": HEAD, "omnimarket#3302": HEAD},
        {"a#1": "open"},
    )
    assert set(result.waits_open) == {"omnimarket#3301"}


def test_keys_are_lowercase_and_a_cased_pr_matches() -> None:
    result = run(
        [
            waiting(
                "2026-10-09T09:00:00Z",
                "OmniMarket#3301",
                "head=aaaaaaaaaaaa | parents=a#1",
            )
        ],
        {"OmniMarket#3301": HEAD},
        {"a#1": "open"},
    )
    assert list(result.waits_open) == ["omnimarket#3301"]


def test_a_later_msg_naming_a_fired_wait_is_reported_unless_a_watcher_wrote_it() -> (
    None
):
    parked = waiting(
        "2026-10-09T09:00:00Z", "omnimarket#3301", "head=aaaaaaaaaaaa | parents=a#1"
    )
    states = {"a#1": "merged"}
    by_watcher = row(
        "2026-10-09T09:10:00Z",
        "MSG",
        "landing-controller",
        "to=repo-drains-1 | omnimarket#3301 may go",
    )
    assert (
        run([parked, by_watcher], {"omnimarket#3301": HEAD}, states).msg_after_wait
        == ()
    )
    by_lane = row(
        "2026-10-09T09:11:00Z",
        "MSG",
        "land-b",
        "to=repo-drains-1 | OmniNode-ai/omnimarket#3301 may go",
    )
    assert run(
        [parked, by_watcher, by_lane], {"omnimarket#3301": HEAD}, states
    ).msg_after_wait == ("omnimarket#3301",)
    earlier = row("2026-10-09T08:59:00Z", "MSG", "land-b", "omnimarket#3301 may go")
    assert (
        run([earlier, parked], {"omnimarket#3301": HEAD}, states).msg_after_wait == ()
    )
    unrelated = row("2026-10-09T09:11:00Z", "MSG", "land-b", "omnimarket#33011 may go")
    assert (
        run([parked, unrelated], {"omnimarket#3301": HEAD}, states).msg_after_wait == ()
    )
    still_open = run([parked, by_lane], {"omnimarket#3301": HEAD}, {"a#1": "open"})
    assert still_open.msg_after_wait == ()


def test_a_naive_clock_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelLandingOrderWaitsRequest(
            now=datetime(2026, 10, 9, 10, 0, 0), rows=(), prs=()
        )


def test_the_result_is_deterministic_and_the_request_is_not_mutated() -> None:
    request = ModelLandingOrderWaitsRequest(
        now=NOW,
        rows=(
            waiting(
                "2026-10-09T09:00:00Z",
                "omnimarket#3301",
                "head=aaaaaaaaaaaa | parents=a#1",
            ),
        ),
        prs=(ModelLandingOrderWaitPr(key="omnimarket#3301", head_sha=HEAD),),
        predecessor_states={"a#1": "open"},
    )
    before = request.model_dump_json()
    assert derive_order_waits(request) == derive_order_waits(request)
    assert request.model_dump_json() == before


def test_node_entry_point_wraps_the_handler() -> None:
    assert issubclass(NodePrLandingOrderWaitsCompute, HandlerPrLandingOrderWaits)
    assert list(inspect.signature(HandlerPrLandingOrderWaits.handle).parameters) == [
        "self",
        "request",
    ]
    hints = inspect.get_annotations(HandlerPrLandingOrderWaits.handle, eval_str=True)
    assert hints == {
        "request": ModelLandingOrderWaitsRequest,
        "return": ModelLandingOrderWaitsResult,
    }


def test_contract_declares_the_handler_and_its_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    assert hasattr(module, handler["class"])
    assert handler["input_model"].endswith("ModelLandingOrderWaitsRequest")
    assert contract["output_model"]["name"] == "ModelLandingOrderWaitsResult"
    topics = contract["runtime_dispatch"]
    assert (
        topics["command_topic"]
        == "onex.cmd.omnimarket.pr-landing-order-waits-requested.v1"
    )
    assert topics["terminal_events"] == {
        "success": "onex.evt.omnimarket.pr-landing-order-waits-completed.v1",
        "failure": "onex.evt.omnimarket.pr-landing-order-waits-failed.v1",
    }
    assert contract["descriptor"]["purity"] == "pure"
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["entry_points"]["onex.nodes"][
        "node_pr_landing_order_waits_compute"
    ] == ("omnimarket.nodes.node_pr_landing_order_waits_compute")


def test_handler_reads_no_clock_environment_or_other_nodes() -> None:
    tree = ast.parse(Path(handler_pr_landing_order_waits.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {"os", "subprocess", "socket", "requests", "httpx", "time"}
    source = Path(handler_pr_landing_order_waits.__file__).read_text()
    assert "datetime.now" not in source
    assert "utcnow" not in source
    foreign = [
        n.module
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom)
        and (n.module or "").startswith("omnimarket.nodes.")
        and "node_pr_landing_order_waits_compute" not in (n.module or "")
    ]
    assert foreign == []
