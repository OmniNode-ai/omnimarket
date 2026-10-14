# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20730: a red CI run reaches the lifecycle orchestrator over the bus, and CI keeps it so.

Ruling 2026-10-08T14:27:26Z: red CI emits a typed event on the bus and the sweep orchestrator
triages it on receipt; polling stays as the backstop. The chain is five contract legs:

1. node_pr_state_emit_effect consumes pr-state-observed and routes it to its red-CI detector;
2. node_pr_state_emit_effect publishes ci-run-failed;
3. node_pr_lifecycle_orchestrator consumes ci-run-failed and routes it to the triage handler;
4. node_pr_lifecycle_orchestrator publishes ci-red-triage-decided;
5. node_pr_lifecycle_state_reducer consumes ci-red-triage-decided.

A dev push run that goes red names no PR, so it rides a second chain to the same orchestrator:

6. node_pr_state_emit_effect consumes branch-head and routes it to its dev-head red detector;
7. node_pr_state_emit_effect publishes dev-head-red;
8. node_pr_lifecycle_orchestrator consumes dev-head-red and routes it to the cause-lane dispatcher;
9. node_pr_lifecycle_orchestrator publishes dev-cause-lane-requested and dev-head-red-decided.

``chain_violations`` reads those legs from the node contracts. A dropped subscription, a
dropped publication, a missing route or a handler module that no longer exists is a violation,
and the planted-violation tests prove each leg is refused. The wiring tests prove the required
``lint`` job and the pre-commit hook run this file, check-only.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.events.topics import (
    CI_RED_TRIAGE_DECIDED_TOPIC_V1,
    CI_RUN_FAILED_TOPIC_V1,
    DEV_CAUSE_LANE_REQUESTED_TOPIC_V1,
    DEV_HEAD_RED_DECIDED_TOPIC_V1,
    DEV_HEAD_RED_TOPIC_V1,
    GITHUB_BRANCH_HEAD_TOPIC_V1,
    PR_STATE_OBSERVED_TOPIC_V1,
)

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_NODES = _ROOT / "src" / "omnimarket" / "nodes"
_EMITTER = "node_pr_state_emit_effect"
_ORCHESTRATOR = "node_pr_lifecycle_orchestrator"
_REDUCER = "node_pr_lifecycle_state_reducer"
_THIS_TEST = "tests/ci/test_red_ci_bus_chain_omn20730.py"
_HOOK_ID = "red-ci-event-chain"


def _load_contracts() -> dict[str, dict[str, Any]]:
    return {
        node: yaml.safe_load((_NODES / node / "contract.yaml").read_text("utf-8"))
        for node in (_EMITTER, _ORCHESTRATOR, _REDUCER)
    }


def _subscribes(contract: dict[str, Any], topic: str) -> bool:
    return topic in (contract.get("event_bus") or {}).get("subscribe_topics", [])


def _publishes(contract: dict[str, Any], topic: str) -> bool:
    bus = contract.get("event_bus") or {}
    declared = [event.get("topic") for event in contract.get("published_events", [])]
    return topic in bus.get("publish_topics", []) and topic in declared


def _routed_handler(contract: dict[str, Any], topic: str) -> dict[str, Any] | None:
    for entry in (contract.get("handler_routing") or {}).get("handlers", []):
        if entry.get("topic") == topic:
            return entry.get("handler") or {}
    return None


def _handler_module_exists(handler: dict[str, Any]) -> bool:
    module = str(handler.get("module", ""))
    return (
        bool(module)
        and (_ROOT / "src" / Path(*module.split(".")).with_suffix(".py")).is_file()
    )


def chain_violations(contracts: dict[str, dict[str, Any]]) -> list[str]:
    """Every leg of the red-CI chain the contracts no longer carry; empty when the chain holds."""
    violations: list[str] = []
    emitter = contracts[_EMITTER]
    orchestrator = contracts[_ORCHESTRATOR]
    reducer = contracts[_REDUCER]

    if not _subscribes(emitter, PR_STATE_OBSERVED_TOPIC_V1):
        violations.append(
            f"{_EMITTER} does not subscribe to {PR_STATE_OBSERVED_TOPIC_V1}"
        )
    detector = _routed_handler(emitter, PR_STATE_OBSERVED_TOPIC_V1)
    if detector is None or not _handler_module_exists(detector):
        violations.append(
            f"{_EMITTER} routes {PR_STATE_OBSERVED_TOPIC_V1} to no existing handler"
        )
    if not _publishes(emitter, CI_RUN_FAILED_TOPIC_V1):
        violations.append(f"{_EMITTER} does not publish {CI_RUN_FAILED_TOPIC_V1}")

    if not _subscribes(orchestrator, CI_RUN_FAILED_TOPIC_V1):
        violations.append(
            f"{_ORCHESTRATOR} does not subscribe to {CI_RUN_FAILED_TOPIC_V1}"
        )
    triage = _routed_handler(orchestrator, CI_RUN_FAILED_TOPIC_V1)
    if triage is None or not _handler_module_exists(triage):
        violations.append(
            f"{_ORCHESTRATOR} routes {CI_RUN_FAILED_TOPIC_V1} to no existing handler"
        )
    if not _publishes(orchestrator, CI_RED_TRIAGE_DECIDED_TOPIC_V1):
        violations.append(
            f"{_ORCHESTRATOR} does not publish {CI_RED_TRIAGE_DECIDED_TOPIC_V1}"
        )

    if not _subscribes(reducer, CI_RED_TRIAGE_DECIDED_TOPIC_V1):
        violations.append(
            f"{_REDUCER} does not subscribe to {CI_RED_TRIAGE_DECIDED_TOPIC_V1}"
        )

    for node, contract, consumed in (
        (_EMITTER, emitter, GITHUB_BRANCH_HEAD_TOPIC_V1),
        (_ORCHESTRATOR, orchestrator, DEV_HEAD_RED_TOPIC_V1),
    ):
        if not _subscribes(contract, consumed):
            violations.append(f"{node} does not subscribe to {consumed}")
        routed = _routed_handler(contract, consumed)
        if routed is None or not _handler_module_exists(routed):
            violations.append(f"{node} routes {consumed} to no existing handler")
    for node, contract, published in (
        (_EMITTER, emitter, DEV_HEAD_RED_TOPIC_V1),
        (_ORCHESTRATOR, orchestrator, DEV_CAUSE_LANE_REQUESTED_TOPIC_V1),
        (_ORCHESTRATOR, orchestrator, DEV_HEAD_RED_DECIDED_TOPIC_V1),
    ):
        if not _publishes(contract, published):
            violations.append(f"{node} does not publish {published}")
    return violations


def test_the_live_contracts_carry_the_whole_chain() -> None:
    assert chain_violations(_load_contracts()) == []


def _drop_subscription(node: str, topic: str) -> dict[str, dict[str, Any]]:
    contracts = copy.deepcopy(_load_contracts())
    bus = contracts[node]["event_bus"]
    bus["subscribe_topics"] = [t for t in bus["subscribe_topics"] if t != topic]
    return contracts


def _drop_publication(node: str, topic: str) -> dict[str, dict[str, Any]]:
    contracts = copy.deepcopy(_load_contracts())
    contracts[node]["event_bus"]["publish_topics"] = [
        t for t in contracts[node]["event_bus"]["publish_topics"] if t != topic
    ]
    return contracts


def _drop_route(node: str, topic: str) -> dict[str, dict[str, Any]]:
    contracts = copy.deepcopy(_load_contracts())
    handlers = contracts[node]["handler_routing"]["handlers"]
    handlers[:] = [h for h in handlers if h.get("topic") != topic]
    return contracts


def _point_route_at_a_missing_module(
    node: str, topic: str
) -> dict[str, dict[str, Any]]:
    contracts = copy.deepcopy(_load_contracts())
    for entry in contracts[node]["handler_routing"]["handlers"]:
        if entry.get("topic") == topic:
            entry["handler"]["module"] = "omnimarket.nodes.handler_that_was_deleted"
    return contracts


_PLANTED = {
    "emitter stops consuming pr-state-observed": (
        _drop_subscription(_EMITTER, PR_STATE_OBSERVED_TOPIC_V1),
        f"{_EMITTER} does not subscribe to {PR_STATE_OBSERVED_TOPIC_V1}",
    ),
    "emitter loses its detector route": (
        _drop_route(_EMITTER, PR_STATE_OBSERVED_TOPIC_V1),
        f"{_EMITTER} routes {PR_STATE_OBSERVED_TOPIC_V1} to no existing handler",
    ),
    "emitter stops publishing ci-run-failed": (
        _drop_publication(_EMITTER, CI_RUN_FAILED_TOPIC_V1),
        f"{_EMITTER} does not publish {CI_RUN_FAILED_TOPIC_V1}",
    ),
    "orchestrator stops consuming ci-run-failed": (
        _drop_subscription(_ORCHESTRATOR, CI_RUN_FAILED_TOPIC_V1),
        f"{_ORCHESTRATOR} does not subscribe to {CI_RUN_FAILED_TOPIC_V1}",
    ),
    "orchestrator loses its triage route": (
        _drop_route(_ORCHESTRATOR, CI_RUN_FAILED_TOPIC_V1),
        f"{_ORCHESTRATOR} routes {CI_RUN_FAILED_TOPIC_V1} to no existing handler",
    ),
    "orchestrator triage route names a deleted module": (
        _point_route_at_a_missing_module(_ORCHESTRATOR, CI_RUN_FAILED_TOPIC_V1),
        f"{_ORCHESTRATOR} routes {CI_RUN_FAILED_TOPIC_V1} to no existing handler",
    ),
    "orchestrator stops publishing the decision": (
        _drop_publication(_ORCHESTRATOR, CI_RED_TRIAGE_DECIDED_TOPIC_V1),
        f"{_ORCHESTRATOR} does not publish {CI_RED_TRIAGE_DECIDED_TOPIC_V1}",
    ),
    "reducer stops consuming the decision": (
        _drop_subscription(_REDUCER, CI_RED_TRIAGE_DECIDED_TOPIC_V1),
        f"{_REDUCER} does not subscribe to {CI_RED_TRIAGE_DECIDED_TOPIC_V1}",
    ),
    "emitter stops consuming branch-head": (
        _drop_subscription(_EMITTER, GITHUB_BRANCH_HEAD_TOPIC_V1),
        f"{_EMITTER} does not subscribe to {GITHUB_BRANCH_HEAD_TOPIC_V1}",
    ),
    "emitter loses its dev-head red detector route": (
        _drop_route(_EMITTER, GITHUB_BRANCH_HEAD_TOPIC_V1),
        f"{_EMITTER} routes {GITHUB_BRANCH_HEAD_TOPIC_V1} to no existing handler",
    ),
    "emitter stops publishing dev-head-red": (
        _drop_publication(_EMITTER, DEV_HEAD_RED_TOPIC_V1),
        f"{_EMITTER} does not publish {DEV_HEAD_RED_TOPIC_V1}",
    ),
    "orchestrator stops consuming dev-head-red": (
        _drop_subscription(_ORCHESTRATOR, DEV_HEAD_RED_TOPIC_V1),
        f"{_ORCHESTRATOR} does not subscribe to {DEV_HEAD_RED_TOPIC_V1}",
    ),
    "orchestrator dispatch route names a deleted module": (
        _point_route_at_a_missing_module(_ORCHESTRATOR, DEV_HEAD_RED_TOPIC_V1),
        f"{_ORCHESTRATOR} routes {DEV_HEAD_RED_TOPIC_V1} to no existing handler",
    ),
    "orchestrator stops asking for the cause lane": (
        _drop_publication(_ORCHESTRATOR, DEV_CAUSE_LANE_REQUESTED_TOPIC_V1),
        f"{_ORCHESTRATOR} does not publish {DEV_CAUSE_LANE_REQUESTED_TOPIC_V1}",
    ),
    "orchestrator stops publishing the dev-head decision": (
        _drop_publication(_ORCHESTRATOR, DEV_HEAD_RED_DECIDED_TOPIC_V1),
        f"{_ORCHESTRATOR} does not publish {DEV_HEAD_RED_DECIDED_TOPIC_V1}",
    ),
}


@pytest.mark.parametrize("planted", sorted(_PLANTED))
def test_a_planted_violation_is_refused(planted: str) -> None:
    contracts, expected = _PLANTED[planted]
    assert expected in chain_violations(contracts)


def _hook() -> dict[str, Any]:
    config = yaml.safe_load((_ROOT / ".pre-commit-config.yaml").read_text("utf-8"))
    hooks = [
        h for repo in config["repos"] for h in repo["hooks"] if h["id"] == _HOOK_ID
    ]
    assert len(hooks) == 1
    return hooks[0]


def test_pre_commit_hook_runs_this_file_check_only() -> None:
    hook = _hook()
    assert _THIS_TEST in hook["entry"]
    assert hook["entry"].startswith("uv run pytest ")
    assert hook["pass_filenames"] is False
    assert "pre-commit" in hook["stages"]


def test_hook_files_filter_selects_the_chain_inputs() -> None:
    files = _hook()["files"]
    for path in (
        _THIS_TEST,
        f"src/omnimarket/nodes/{_EMITTER}/contract.yaml",
        f"src/omnimarket/nodes/{_ORCHESTRATOR}/contract.yaml",
        f"src/omnimarket/nodes/{_REDUCER}/contract.yaml",
        "src/omnimarket/events/topics.py",
    ):
        assert re.search(files, path), path


def test_required_lint_job_runs_this_file() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8")
    )
    runs = [str(step.get("run", "")) for step in workflow["jobs"]["lint"]["steps"]]
    assert any(_THIS_TEST in run and "pytest" in run for run in runs)
