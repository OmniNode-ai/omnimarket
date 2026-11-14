# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

import copy
import dataclasses
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import BaseModel

from scripts.audit.market_node_runtime_dogfood import (
    _NODES_DIR,
    _PYPROJECT,
    _local_invocation_check,
    build_report,
)
from scripts.validate_node_drift import load_entry_point_exemptions, validate_node

FOCUS_NODES = {
    "node_contract_reducer",
    "node_cross_cli_originator",
    "node_e2e_orchestrator",
    "node_finding_aggregator_compute",
    "node_intent_storage_effect",
    "node_llm_delegation_routing_compute",
    "node_memory_storage_effect",
    "node_model_router",
    "node_navigation_history_reducer",
    "node_overseer_observer",
    "node_persona_builder_compute",
    "node_persona_retrieval_effect",
    "node_persona_storage_effect",
    "node_polish_task_classifier",
    "node_projection_dep_health",
    "node_rsd_fill_compute",
    "node_semantic_analyzer_compute",
    "node_similarity_compute",
    "node_ticket_classify_compute",
}

NATIVE_NON_ADDRESSABLE_NODES = {
    "node_navigation_history_reducer",
    "node_projection_dep_health",
}

EVENT_ROUTED_NODES = {"node_e2e_orchestrator"}

# The reviewed list node-drift-gate reads too (scripts/audit/
# node_entry_point_exemptions.yaml): each entry carries the reason it has no
# onex.nodes entry point.
EXPECTED_MISSING_ENTRY_POINTS = set(load_entry_point_exemptions())

# Node directories that hold migrations and no contract.yaml yet. Each entry
# expires itself: a directory that gains a contract.yaml, or disappears, fails
# the inventory until the entry is removed.
MIGRATION_ONLY_NODE_DIRS: set[str] = set()

# Node directories on dev when the pinned totals were retired (OMN-17427).
# Adding a node never touches this. Lower it only in a PR that deletes a node,
# so a node that disappears together with its entry point still fails here.
_NODE_DIR_FLOOR = 446


@dataclasses.dataclass(frozen=True)
class _Inventory:
    node_dirs: frozenset[str]
    # entry-point name -> its target ("omnimarket.nodes.<node>")
    entry_points: dict[str, str]
    # node dir -> the contract's `name`, or None when contract.yaml is absent
    contract_names: dict[str, str | None]


def _real_inventory() -> _Inventory:
    node_dirs = frozenset(
        item.name
        for item in _NODES_DIR.iterdir()
        if item.is_dir() and item.name.startswith("node_")
    )
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    entry_points = {
        str(name): str(target)
        for name, target in pyproject["project"]["entry-points"]["onex.nodes"].items()
    }
    contract_names: dict[str, str | None] = {}
    for node in node_dirs:
        contract_path = _NODES_DIR / node / "contract.yaml"
        if not contract_path.is_file():
            contract_names[node] = None
            continue
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        name = raw.get("name") if isinstance(raw, dict) else None
        contract_names[node] = str(name) if name is not None else ""
    return _Inventory(node_dirs, entry_points, contract_names)


def _inventory_violations(inventory: _Inventory) -> list[str]:
    """Every way the node inventory can be missing or carry an extra node.

    Derived from the three registries a node has (its directory, its
    contract.yaml, its onex.nodes entry point) plus the reviewed
    EXPECTED_MISSING_ENTRY_POINTS set, so a PR that adds a node consistently
    passes without editing this file, and one that adds or drops only part of
    a node fails.
    """
    dirs = inventory.node_dirs
    entries = set(inventory.entry_points)
    violations: list[str] = []
    for node in sorted(dirs - entries - EXPECTED_MISSING_ENTRY_POINTS):
        violations.append(f"{node}: node directory has no onex.nodes entry point")
    for node in sorted(entries - dirs):
        violations.append(f"{node}: onex.nodes entry point has no node directory")
    for node in sorted(EXPECTED_MISSING_ENTRY_POINTS - dirs):
        violations.append(f"{node}: listed as expected-missing but not on disk")
    for node in sorted(EXPECTED_MISSING_ENTRY_POINTS & entries):
        violations.append(f"{node}: listed as expected-missing but has an entry point")
    for node, target in sorted(inventory.entry_points.items()):
        if target.split(":", 1)[0] != f"omnimarket.nodes.{node}":
            violations.append(f"{node}: entry point targets {target}")
    for node in sorted(MIGRATION_ONLY_NODE_DIRS - dirs):
        violations.append(f"{node}: listed as migration-only but not on disk")
    for node in sorted(dirs):
        name = inventory.contract_names.get(node)
        if node in MIGRATION_ONLY_NODE_DIRS:
            if name is not None:
                violations.append(
                    f"{node}: listed as migration-only but has a contract"
                )
        elif name is None:
            violations.append(f"{node}: node directory has no contract.yaml")
        elif name not in {node, node.removeprefix("node_")}:
            violations.append(f"{node}: contract.yaml names {name!r}")
    return violations


@pytest.mark.parametrize("listed", [False, True])
@pytest.mark.parametrize("lifecycle", ["", "experimental", "deprecated"])
def test_gate_and_inventory_agree_on_every_entry_point_case(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lifecycle: str,
    listed: bool,
) -> None:
    """node-drift-gate and the inventory test judge a node with no entry point alike.

    Control: before the gate read the reviewed list, an unlisted experimental or
    deprecated node passed the gate while the inventory test flagged it, so a
    pull request that added one went green at the gate and red at the test.
    """
    node_dir = tmp_path / "node_case"
    node_dir.mkdir()
    lifecycle_line = f"lifecycle: {lifecycle}\n" if lifecycle else ""
    (node_dir / "contract.yaml").write_text(
        "name: node_case\nnode_type: effect\n"
        f"{lifecycle_line}"
        "handler:\n"
        "  module: omnimarket.nodes.node_case.handlers.handler_case\n"
        "  class: HandlerCase\n"
    )
    exemptions = {"node_case"} if listed else set[str]()

    gate_result = validate_node(
        node_dir, entry_points=set(), strict=True, exemptions=exemptions
    )
    gate_flags = any(
        f.check == "pyproject_entry" and f.level == "FAIL" for f in gate_result.findings
    )

    monkeypatch.setattr(
        "tests.test_market_node_runtime_dogfood.EXPECTED_MISSING_ENTRY_POINTS",
        exemptions,
    )
    inventory = _Inventory(
        node_dirs=frozenset({"node_case"}),
        entry_points={},
        contract_names={"node_case": "node_case"},
    )
    inventory_flags = any(
        "no onex.nodes entry point" in violation
        for violation in _inventory_violations(inventory)
    )

    assert gate_flags == inventory_flags
    assert gate_flags is (not listed)


def test_real_gate_and_inventory_read_one_list() -> None:
    assert set(load_entry_point_exemptions()) == EXPECTED_MISSING_ENTRY_POINTS
    assert _inventory_violations(_real_inventory()) == []


def _with_stray_dir(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(
        inv,
        node_dirs=inv.node_dirs | {"node_zz_stray"},
        contract_names={**inv.contract_names, "node_zz_stray": None},
    )


def _with_dangling_entry_point(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(
        inv,
        entry_points={
            **inv.entry_points,
            "node_zz_ghost": "omnimarket.nodes.node_zz_ghost",
        },
    )


def _without_node_dir(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(
        inv, node_dirs=inv.node_dirs - {"node_similarity_compute"}
    )


def _without_entry_point(inv: _Inventory) -> _Inventory:
    entry_points = dict(inv.entry_points)
    del entry_points["node_similarity_compute"]
    return dataclasses.replace(inv, entry_points=entry_points)


def _with_wrong_contract_name(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(
        inv,
        contract_names={**inv.contract_names, "node_similarity_compute": "other"},
    )


def _with_wrong_entry_target(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(
        inv,
        entry_points={
            **inv.entry_points,
            "node_similarity_compute": "omnimarket.nodes.node_model_router",
        },
    )


def _without_expected_missing_dir(inv: _Inventory) -> _Inventory:
    return dataclasses.replace(inv, node_dirs=inv.node_dirs - {"node_dev_seed_effect"})


@pytest.mark.parametrize(
    "mutate",
    [
        _with_stray_dir,
        _with_dangling_entry_point,
        _without_node_dir,
        _without_entry_point,
        _with_wrong_contract_name,
        _with_wrong_entry_target,
        _without_expected_missing_dir,
    ],
)
def test_market_node_inventory_fails_on_a_missing_or_extra_node(
    mutate: Callable[[_Inventory], _Inventory],
) -> None:
    # Positive control: each single-registry drift is caught by the derived
    # checks that replaced the pinned totals.
    assert _inventory_violations(_real_inventory()) == []
    assert _inventory_violations(mutate(_real_inventory())) != []


def _real_contracts() -> dict[str, dict[str, Any]]:
    """Contract of every node that has an onex.nodes entry point."""
    entries = _real_inventory().entry_points
    contracts: dict[str, dict[str, Any]] = {}
    for node in entries:
        contract_path = _NODES_DIR / node / "contract.yaml"
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        contracts[node] = raw if isinstance(raw, dict) else {}
    return contracts


def _declared_unrouted(contracts: dict[str, dict[str, Any]]) -> set[str]:
    """Nodes whose own contract says they have no handler route yet.

    Experimental lifecycle, no handler_routing, not a typed local invocation and
    still runtime addressable: the set the dogfood report skips as
    experimental_handler_pending.
    """
    return {
        node
        for node, contract in contracts.items()
        if contract.get("lifecycle") == "experimental"
        and "handler_routing" not in contract
        and not _local_invocation_check(contract)[0]
        and (contract.get("runtime_dispatch") or {}).get("addressable") is not False
    }


def _subscribed_topics(contract: dict[str, Any]) -> list[object]:
    event_bus = contract.get("event_bus") or {}
    return [
        topic
        for source in (
            event_bus.get("subscribe_topics"),
            event_bus.get("subscribe"),
            contract.get("subscribe_topics"),
            contract.get("subscribe_topic"),
        )
        for topic in ([source] if isinstance(source, str) else source or [])
    ]


def _unrouted_violations(contracts: dict[str, dict[str, Any]]) -> list[str]:
    """A node without handler_routing must be one nothing on the bus can reach.

    With the total no longer pinned, this is what fails when a routable node is
    left experimental and loses its handler_routing: it still subscribes to a
    command topic, so the bus would deliver to a node with no handler.
    """
    return [
        f"{node}: has no handler_routing but subscribes to {topics}"
        for node in sorted(_declared_unrouted(contracts))
        if (topics := _subscribed_topics(contracts[node]))
    ]


def _with_routable_node_unrouted(
    contracts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node = copy.deepcopy(contracts["node_similarity_compute"])
    node["lifecycle"] = "experimental"
    del node["handler_routing"]
    return {**contracts, "node_similarity_compute": node}


def _with_unrouted_node_subscribed(
    contracts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    node = copy.deepcopy(contracts["node_lab_job_reducer"])
    node["event_bus"] = {
        **(node.get("event_bus") or {}),
        "subscribe_topics": ["onex.cmd.omnimarket.lab-job-reduce.v1"],
    }
    return {**contracts, "node_lab_job_reducer": node}


@pytest.mark.parametrize(
    "mutate",
    [_with_routable_node_unrouted, _with_unrouted_node_subscribed],
)
def test_unrouted_node_that_a_topic_reaches_fails(
    mutate: Callable[
        [dict[str, dict[str, Any]]],
        dict[str, dict[str, Any]],
    ],
) -> None:
    # Positive control: the real contracts are clean, and each injected break
    # (a routable node left experimental with its handler_routing removed, an
    # unrouted node given a subscribe topic) is reported.
    assert _unrouted_violations(_real_contracts()) == []
    assert _unrouted_violations(mutate(_real_contracts())) != []


def test_migration_only_dir_fails_once_it_gains_a_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No directory is migration-only on this head, so the self-expiring entry
    # is exercised against a directory that does have a contract: listing it
    # must be reported.
    node = "node_similarity_compute"
    monkeypatch.setitem(globals(), "MIGRATION_ONLY_NODE_DIRS", {node})
    violations = _inventory_violations(_real_inventory())
    assert f"{node}: listed as migration-only but has a contract" in violations


def test_market_node_runtime_dogfood_inventory_classifies_all_entry_points() -> None:
    report = build_report()
    summary = report["summary"]

    # OMN-17427: the node and entry-point totals are derived, never pinned.
    # A pinned total was edited by every node-migration PR, so each merge
    # re-conflicted every other open migration PR on these two lines. The
    # set checks in _inventory_violations are what fail on a missing or
    # extra node; the totals below follow from them.
    assert _inventory_violations(_real_inventory()) == []
    assert summary["node_dirs"] >= _NODE_DIR_FLOOR
    assert summary["entry_points"] == summary["node_dirs"] - len(
        EXPECTED_MISSING_ENTRY_POINTS
    )
    assert set(summary["missing_entry_points"]) == EXPECTED_MISSING_ENTRY_POINTS
    assert summary["dangling_entry_points"] == []
    assert summary["routable"] >= 299
    # OMN-22571: the skipped total is derived, never pinned. A pinned total was
    # edited by every pull request that added an experimental node with no
    # handler_routing, so each merge turned every other open one red. The set
    # below is read from the contracts; _unrouted_violations is what fails when
    # a node that a bus topic can reach loses its handler_routing.
    contracts = _real_contracts()
    unrouted = _declared_unrouted(contracts)
    assert _unrouted_violations(contracts) == []
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "experimental_handler_pending"
    } == unrouted
    assert summary["skipped"] == len(unrouted) + len(
        [item for item in report["skipped"] if item["bucket"] == "non_addressable"]
    )
    assert summary["failed"] == 0
    assert summary["failure_buckets"] == {}
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "non_addressable"
    } == {
        "node_merge_state_projection",
        "node_navigation_history_reducer",
        "node_projection_dep_health",
        "node_pr_merged_projection",  # OMN-13226 T2 stub; handler pending T3
        "node_view_seam_reading_reducer",  # OMN-22020 projection reducer
    }
    assert not {
        item["node_name"]
        for item in report["skipped"]
        if item["node_name"].endswith("_compute")
    }


def test_market_node_runtime_dogfood_proves_nested_contract_shapes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_projection_delegation_disposition"]["input_model"]
        == "omnimarket.nodes.node_projection_delegation_disposition.models.model_delegation_disposition.ModelDelegationDispositionProjectionRequest"
    )
    assert (
        routable["node_projection_delegation_disposition"]["handler"]
        == "omnimarket.nodes.node_projection_delegation_disposition.handlers.handler_projection_delegation_disposition.HandlerProjectionDelegationDisposition"
    )

    assert (
        routable["node_ab_compare_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start.ModelAbCompareStart"
    )
    assert (
        routable["node_loop_state_reducer"]["handler"]
        == "omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state.HandlerLoopState"
    )
    assert (
        routable["node_adr_canary_orchestrator"]["input_model"]
        == "omnimarket.nodes.node_adr_canary_orchestrator.models.model_canary_request.ModelCanaryCommandPayload"
    )
    assert (
        routable["node_session_compose"]["command_topic"]
        == "onex.cmd.omnimarket.session-compose.v1"
    )
    assert (
        routable["node_ticket_query"]["terminal_topic"]
        == "onex.evt.omnimarket.ticket-query-completed.v1"
    )
    assert (
        routable["node_dirty_canonical_sweep"]["command_topic"]
        == "onex.cmd.omnimarket.dirty-canonical-sweep.v1"
    )
    assert (
        routable["node_code_embedding_effect"]["input_model"]
        == "omnimarket.nodes.node_code_embedding_effect.models.model_code_embedding_request.ModelCodeEmbeddingRequest"
    )
    assert (
        routable["node_emit_daemon"]["input_model"]
        == "omnimarket.nodes.node_emit_daemon.models.model_daemon_state.ModelEmitDaemonCommand"
    )
    assert (
        routable["node_emit_daemon"]["command_topic"]
        == "onex.cmd.omnimarket.emit-daemon-lifecycle.v1"
    )
    assert (
        routable["node_similarity_compute"]["command_topic"]
        == "onex.cmd.omnimemory.similarity-compute.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["command_topic"]
        == "onex.cmd.omnimarket.projection-replay-check-start.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["terminal_topic"]
        == "onex.evt.omnimarket.projection-replay-check-completed.v1"
    )
    assert (
        routable["node_projection_replay_check_compute"]["input_model"]
        == "omnimarket.nodes.node_projection_replay_check_compute.models.model_replay_check.ModelReplayCheckRequest"
    )
    assert (
        routable["node_projection_replay_check_compute"]["handler"]
        == "omnimarket.nodes.node_projection_replay_check_compute.handlers.handler_projection_replay_check.HandlerProjectionReplayCheck"
    )
    assert (
        routable["node_memory_storage_effect"]["command_topic"]
        == "onex.cmd.omnimemory.memory-storage.v1"
    )
    assert (
        routable["node_overseer_observer"]["input_model"]
        == "omnimarket.nodes.node_overseer_observer.models.model_overseer_observation_request.ModelOverseerObservationRequest"
    )


def test_market_node_runtime_dogfood_proves_canonical_handler_repairs() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}

    assert (
        routable["node_agent_coordinator_orchestrator"]["handler"]
        == "omnimemory.handlers.handler_subscription.HandlerSubscription"
    )
    assert (
        routable["node_memory_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_memory_lifecycle_orchestrator.handlers.handler_memory_tick.HandlerMemoryTick"
    )
    assert (
        routable["node_persona_lifecycle_orchestrator"]["handler"]
        == "omnimarket.nodes.node_persona_lifecycle_orchestrator.handlers.handler_persona_rebuild.HandlerPersonaRebuild"
    )


def test_market_node_runtime_dogfood_audits_former_non_addressable_nodes() -> None:
    report = build_report()
    routable = {item["node_name"]: item for item in report["routable"]}
    skipped = {item["node_name"]: item for item in report["skipped"]}
    failures = {item["node_name"]: item for item in report["failures"]}

    assert failures.keys().isdisjoint(FOCUS_NODES)
    assert (set(routable) | set(skipped)) & FOCUS_NODES == FOCUS_NODES
    assert set(skipped) & FOCUS_NODES == NATIVE_NON_ADDRESSABLE_NODES

    command_addressable_nodes = (
        FOCUS_NODES - NATIVE_NON_ADDRESSABLE_NODES - EVENT_ROUTED_NODES
    )
    assert command_addressable_nodes <= set(routable)
    # Entered by an event, not a command: the audit's command_topic is its first
    # subscribe topic, which is the upstream workflow's completion event.
    assert set(routable) >= EVENT_ROUTED_NODES
    assert all(
        routable[node_name]["command_topic"].startswith("onex.evt.")
        for node_name in EVENT_ROUTED_NODES
    )
    assert all(
        routable[node_name]["command_topic"].startswith("onex.cmd.")
        for node_name in command_addressable_nodes
    )


class _LocalRequest(BaseModel):
    pass


class _LocalResult(BaseModel):
    passed: bool = True


class _LocalHandler:
    def handle(self, request: _LocalRequest) -> _LocalResult:
        return _LocalResult()


def _local_contract() -> dict[str, object]:
    return {
        "metadata": {"transport_type": "local"},
        "runtime_dispatch": {"external_trigger": "CLI in the target worktree"},
        "descriptor": {"runtime_profiles": []},
        "event_bus": {"subscribe_topics": [], "publish_topics": []},
        "handler": {
            "module": __name__,
            "class": "_LocalHandler",
            "input_model": f"{__name__}._LocalRequest",
            "output_model": f"{__name__}._LocalResult",
        },
    }


def test_local_invocation_requires_a_resolvable_typed_handler() -> None:
    assert _local_invocation_check(_local_contract()) == (True, "")
    broken = _local_contract()
    broken["handler"]["class"] = "MissingHandler"
    claimed, error = _local_invocation_check(broken)
    assert claimed
    assert error
    broken = _local_contract()
    broken["handler"]["input_model"] = f"{__name__}._LocalResult"
    claimed, error = _local_invocation_check(broken)
    assert claimed
    assert "declared request/result" in error


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("runtime_dispatch", {}),
        ("event_bus", {"subscribe_topics": ["unexpected"], "publish_topics": []}),
        ("descriptor", {"runtime_profiles": ["dev"]}),
    ],
)
def test_local_transport_does_not_excuse_a_malformed_invocation(
    key: str, value: object
) -> None:
    contract = _local_contract()
    contract[key] = value
    claimed, error = _local_invocation_check(contract)
    assert claimed
    assert error


def test_inventory_accounts_for_local_invocation_and_refuses_broken_handler(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.audit import market_node_runtime_dogfood as audit

    node = "node_local_example_effect"
    directory = tmp_path / node
    directory.mkdir()
    contract = _local_contract()
    path = directory / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    monkeypatch.setattr(audit, "_NODES_DIR", tmp_path)
    monkeypatch.setattr(audit, "_load_entry_points", lambda: [node])
    report = audit.build_report()
    assert report["summary"]["local_invokable"] == 1
    assert report["summary"]["failed"] == 0
    assert report["summary"]["skipped"] == 0
    assert report["local_invokable"] == [{"command_name": node, "node_name": node}]
    contract["handler"]["class"] = "MissingHandler"
    path.write_text(yaml.safe_dump(contract))
    report = audit.build_report()
    assert report["summary"]["local_invokable"] == 0
    assert report["summary"]["failed"] == 1
    assert report["summary"]["failure_buckets"] == {"invalid_local_invocation": 1}


def _declared_entry_point_nodes_that_subscribe_without_registration() -> list[str]:
    """Nodes whose contract subscribes to a bus topic and whose metadata.yaml
    declares an onex.nodes entry point, yet pyproject.toml has no such line.

    The runtime finds nodes only through the onex.nodes table, so such a node
    is never loaded: its command topic is never subscribed and its result topic
    is never created (OMN-18925). An EXPECTED_MISSING_ENTRY_POINTS reason that
    says "subscribes to no topic" cannot be true for it.
    """
    with _PYPROJECT.open("rb") as f:
        registered = set(
            tomllib.load(f)["project"]["entry-points"]["onex.nodes"].keys()
        )
    offenders: list[str] = []
    for meta_path in sorted(_NODES_DIR.glob("*/metadata.yaml")):
        node_dir = meta_path.parent
        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8")) or {}
        declared = (meta.get("entry_points") or {}).get("onex.nodes") or {}
        contract_path = node_dir / "contract.yaml"
        if not declared or not contract_path.exists():
            continue
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        topics = (contract.get("event_bus") or {}).get("subscribe_topics") or []
        if topics and any(name not in registered for name in declared):
            offenders.append(node_dir.name)
    return offenders


def test_declared_subscribing_node_entry_points_are_registered() -> None:
    offenders = _declared_entry_point_nodes_that_subscribe_without_registration()

    assert offenders == [], (
        "these nodes declare an onex.nodes entry point and subscribe to a bus topic, "
        "but pyproject.toml's onex.nodes table has no line for them, so the runtime "
        "never loads them: " + ", ".join(offenders)
    )


def _http_mounted_nodes_without_registration() -> list[str]:
    """Nodes whose contract declares an http_mount but whose pyproject.toml
    onex.nodes table has no entry point for them (OMN-22341).

    The runtime mounts a contract's http_mount only for a contract it
    discovered through the onex.nodes table, so such a node's HTTP route is
    never discovered and never mounted.
    """
    with _PYPROJECT.open("rb") as f:
        registered = set(
            tomllib.load(f)["project"]["entry-points"]["onex.nodes"].keys()
        )
    offenders: list[str] = []
    for contract_path in sorted(_NODES_DIR.glob("*/contract.yaml")):
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
        if not isinstance(contract, dict) or not contract.get("http_mount"):
            continue
        if contract_path.parent.name not in registered:
            offenders.append(contract_path.parent.name)
    return offenders


def test_http_mounted_node_entry_points_are_registered() -> None:
    offenders = _http_mounted_nodes_without_registration()

    assert offenders == [], (
        "these nodes declare an http_mount in their contract, "
        "but pyproject.toml's onex.nodes table has no line for them, so the runtime "
        "never discovers their contract and never mounts them (OMN-22341): "
        + ", ".join(offenders)
    )
