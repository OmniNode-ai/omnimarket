# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 to AC3: a merged PR leaves the watcher's open set within a bounded number of ticks.

Each acceptance test runs against two implementations of one closure tick:

* ``legacy``: the frozen watcher's closure read as it is described for
  ``run_tick``: read only OPEN records that are in neither the search hits nor
  the REST open lists, at most MAX_CLOSE_READS=20 of ``sorted(gone)``, with no
  carry to the next tick. It is marked strict xfail: the test must fail on it.
* ``reconciler``: node_pr_watcher_closure_reconcile_effect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect import (
    NodePrWatcherClosureReconcileEffect,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile import (
    load_policy,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.models.model_pr_watcher_closure_reconcile import (
    ModelPrNodeState,
    ModelPrWatcherClosureReconcileRequest,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.protocols.adapter_github_graphql_node_states import (
    AdapterGithubGraphqlNodeStates,
)
from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.protocols.protocol_pr_node_state_reader import (
    ProtocolPrNodeStateReader,
)

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "omnibase_infra#5555"
MAX_CLOSE_READS = 20


class _Container:
    """The boot container's get_service, holding one reader."""

    def __init__(self, reader: object) -> None:
        self._reader = reader

    def get_service(self, key: Any) -> Any:
        assert key is ProtocolPrNodeStateReader
        return self._reader


def _node(reader: object) -> NodePrWatcherClosureReconcileEffect:
    container: Any = _Container(reader)
    return NodePrWatcherClosureReconcileEffect(container)


class FakeGithub:
    """GitHub's PR states by node id; counts calls."""

    def __init__(self, states: dict[str, ModelPrNodeState]) -> None:
        self.states = states
        self.calls: list[list[str]] = []

    def read_states(
        self, node_ids: Sequence[str]
    ) -> Mapping[str, ModelPrNodeState | None]:
        self.calls.append(list(node_ids))
        return {n: self.states.get(n) for n in node_ids}


TickFn = Callable[[Path, FakeGithub, set[str], set[str], str], None]


def legacy_tick(
    state_path: Path,
    github: FakeGithub,
    search_hits: set[str],
    rest_open: set[str],
    now: str,
) -> None:
    state = json.loads(state_path.read_text())
    gone = [
        k
        for k, r in state["prs"].items()
        if r["facts"]["state"] == "OPEN" and k not in search_hits and k not in rest_open
    ]
    for key in sorted(gone)[:MAX_CLOSE_READS]:
        facts = state["prs"][key]["facts"]
        answer = github.read_states([facts["node_id"]])[facts["node_id"]]
        if answer is not None and answer.state != "OPEN":
            facts["state"] = answer.state
    state_path.write_text(json.dumps(state))


def reconciler_tick(
    state_path: Path,
    github: FakeGithub,
    search_hits: set[str],
    rest_open: set[str],
    now: str,
) -> None:
    # A fresh node per tick: nothing is carried in memory, only on disk.
    _node(github).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(state_path),
            rest_open_keys=tuple(sorted(rest_open)),
            now=now,
        )
    )


IMPLS = [
    pytest.param(
        legacy_tick,
        id="legacy",
        marks=pytest.mark.xfail(strict=True, reason="the current closure logic"),
    ),
    pytest.param(reconciler_tick, id="reconciler"),
]


def _fixture() -> dict[str, Any]:
    return json.loads(
        (FIXTURES / "watcher_record_omnibase_infra_5555.json").read_text()
    )


def _state(tmp_path: Path, extra: dict[str, Any]) -> Path:
    fx = _fixture()
    prs = {fx["key"]: fx["record"], **extra}
    path = tmp_path / "pr-watcher" / "state.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps({"schema": 1, "last_tick": "2026-11-02T15:37:00Z", "prs": prs})
    )
    return path


def _github_with_5555(extra: dict[str, ModelPrNodeState] | None = None) -> FakeGithub:
    fx = _fixture()
    node_id = fx["record"]["facts"]["node_id"]
    states = {
        node_id: ModelPrNodeState(
            node_id=node_id,
            state=fx["github"]["state"],
            merged_at=fx["github"]["merged_at"],
            closed_at=fx["github"]["closed_at"],
        ),
        **(extra or {}),
    }
    return FakeGithub(states)


def _open_record(n: int) -> dict[str, Any]:
    return {"facts": {"number": n, "node_id": f"PR_aaa{n}", "state": "OPEN"}}


def _state_of(path: Path, key: str = KEY) -> str:
    return str(json.loads(path.read_text())["prs"][key]["facts"]["state"])


@pytest.mark.parametrize("tick", IMPLS)
def test_ac1_merged_record_leaves_open_set_while_search_index_lists_it(
    tmp_path: Path, tick: TickFn
) -> None:
    """AC1: the search index still lists #5555 open; the next tick flips it."""
    path = _state(tmp_path, {})
    github = _github_with_5555()
    tick(path, github, {KEY}, set(), "2026-11-02T15:40:00Z")
    assert _state_of(path) == "MERGED"


@pytest.mark.parametrize("tick", IMPLS)
def test_ac2_no_record_is_starved_by_count_or_sort_order(
    tmp_path: Path, tick: TickFn
) -> None:
    """AC2: 470 other gone records sort before #5555; it flips within the bound."""
    others = {f"aaa_repo#{n}": _open_record(n) for n in range(1, 471)}
    path = _state(tmp_path, others)
    github = _github_with_5555(
        {
            f"PR_aaa{n}": ModelPrNodeState(node_id=f"PR_aaa{n}", state="OPEN")
            for n in range(1, 471)
        }
    )
    policy = load_policy()
    per_tick = int(policy["batch_size"]) * int(policy["max_batches_per_tick"])
    bound = -(-471 // per_tick)
    for i in range(bound):
        tick(path, github, set(), set(), f"2026-11-02T15:{40 + i:02d}:00Z")
    assert _state_of(path) == "MERGED"


@pytest.mark.parametrize("tick", IMPLS)
def test_ac3_position_carries_across_a_tick_gap_and_restart(
    tmp_path: Path, tick: TickFn
) -> None:
    """AC3: ticks stop 21:31:23Z to 21:43:31Z across the 21:36:43Z merge.

    The first tick reads while #5555 is still open at GitHub; the merge falls
    in a twelve-minute gap (beyond the five-minute search overlap) and a fresh
    process runs the later ticks. #5555 flips within the full-pass bound.
    """
    others = {f"aaa_repo#{n}": _open_record(n) for n in range(1, 151)}
    path = _state(tmp_path, others)
    github = _github_with_5555(
        {
            f"PR_aaa{n}": ModelPrNodeState(node_id=f"PR_aaa{n}", state="OPEN")
            for n in range(1, 151)
        }
    )
    node_id = _fixture()["record"]["facts"]["node_id"]
    merged = github.states[node_id]
    github.states[node_id] = ModelPrNodeState(node_id=node_id, state="OPEN")
    tick(path, github, set(), set(), "2026-11-01T21:31:23Z")
    assert _state_of(path) == "OPEN"
    github.states[node_id] = merged  # merged at 21:36:43Z, during the gap
    tick(path, github, set(), set(), "2026-11-01T21:43:31Z")
    tick(path, github, set(), set(), "2026-11-01T21:45:31Z")
    assert _state_of(path) == "MERGED"


def test_call_budget_and_bound_are_the_contract_policy(tmp_path: Path) -> None:
    others = {f"aaa_repo#{n}": _open_record(n) for n in range(1, 471)}
    path = _state(tmp_path, others)
    github = _github_with_5555()
    result = _node(github).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(path), now="2026-11-02T15:40:00Z"
        )
    )
    assert result.candidates == 471
    assert result.calls == 2 == len(github.calls)
    assert all(len(ids) == 50 for ids in github.calls)
    assert len(result.read_keys) == 100
    assert result.ticks_to_full_pass == 5
    # The 470 others answered null (unknown to the fake) and stay OPEN.
    assert len(result.unanswered) == 100 - (1 if KEY in result.read_keys else 0)


def test_rest_open_records_are_not_read_and_records_without_node_id_are_named(
    tmp_path: Path,
) -> None:
    path = _state(
        tmp_path,
        {
            "omnimarket#1": _open_record(1),
            "omnimarket#2": {"facts": {"number": 2, "state": "OPEN"}},
        },
    )
    github = _github_with_5555()
    result = _node(github).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(path),
            rest_open_keys=("omnimarket#1",),
            now="2026-11-02T15:40:00Z",
        )
    )
    assert result.read_keys == (KEY,)
    assert result.no_node_id == ("omnimarket#2",)
    assert [f.key for f in result.flipped] == [KEY]
    facts = json.loads(path.read_text())["prs"][KEY]["facts"]
    assert facts["merged_at"] == "2026-11-01T21:36:43Z"
    assert (path.parent / "state.lock").exists()
    cursor = json.loads((path.parent / "closure_reconcile_cursor.json").read_text())
    assert KEY not in cursor["last_read"]  # no longer a candidate


def test_a_failed_batch_flips_nothing_and_keeps_the_record_at_the_head(
    tmp_path: Path,
) -> None:
    path = _state(tmp_path, {})

    class Down:
        def read_states(
            self, node_ids: Sequence[str]
        ) -> Mapping[str, ModelPrNodeState | None]:
            raise OSError("network")

    result = _node(Down()).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(path), now="2026-11-02T15:40:00Z"
        )
    )
    assert result.read_keys == ()
    assert _state_of(path) == "OPEN"
    github = _github_with_5555()
    _node(github).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(path), now="2026-11-02T15:41:00Z"
        )
    )
    assert _state_of(path) == "MERGED"


def test_a_flip_is_not_applied_over_a_record_the_watcher_changed(
    tmp_path: Path,
) -> None:
    path = _state(tmp_path, {})

    class Racing(FakeGithub):
        def read_states(
            self, node_ids: Sequence[str]
        ) -> Mapping[str, ModelPrNodeState | None]:
            got = super().read_states(node_ids)
            state = json.loads(path.read_text())
            state["prs"][KEY]["facts"]["node_id"] = "PR_reopened_as_new"
            path.write_text(json.dumps(state))
            return got

    racing = Racing(_github_with_5555().states)
    result = _node(racing).handle(
        ModelPrWatcherClosureReconcileRequest(
            state_path=str(path), now="2026-11-02T15:40:00Z"
        )
    )
    assert result.flipped == ()
    assert _state_of(path) == "OPEN"


def test_graphql_adapter_is_one_call_per_batch_and_maps_nulls() -> None:
    seen: list[dict[str, Any]] = []

    def call(query: str, variables: dict[str, Any], *, token: str) -> dict[str, Any]:
        seen.append(variables)
        assert "nodes(ids: $ids)" in query
        return {
            "data_unused": None,
            "nodes": [
                {"id": "A", "state": "MERGED", "mergedAt": "t", "closedAt": "t"},
                None,
            ],
        }

    adapter = AdapterGithubGraphqlNodeStates("x", call=call)
    assert isinstance(adapter, ProtocolPrNodeStateReader)
    got = adapter.read_states(["A", "B"])
    assert len(seen) == 1
    answer = got["A"]
    assert answer is not None
    assert answer.state == "MERGED"
    assert got["B"] is None
