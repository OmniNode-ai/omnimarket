# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression coverage for all-node market runtime dogfood inventory."""

from __future__ import annotations

import dataclasses
import tomllib
from collections.abc import Callable

import pytest
import yaml

from scripts.audit.market_node_runtime_dogfood import (
    _NODES_DIR,
    _PYPROJECT,
    build_report,
)

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
    "node_e2e_orchestrator",
    "node_navigation_history_reducer",
    "node_projection_dep_health",
}

EXPECTED_MISSING_ENTRY_POINTS = {
    "node_auto_merge_effect",
    "node_merge_sweep_auto_merge_arm_effect",
    "node_merge_sweep_triage_orchestrator",
    # OMN-17982 validates a supplied historical V4 profile offline.  It is
    # discoverable inventory metadata, but deliberately has no addressable
    # runtime entry point, bus route, or live side-effect capability.
    "node_rsd_v4_static_profile_validate_compute",
    # OMN-17982 V5 is deliberately offline-only: its pure validator has no
    # command/terminal route, event bus, runtime, or effect capability.
    "node_rsd_v5_oci_safe_config_validate_compute",
    # The V4 artifact-evidence verifier is likewise an offline inventory
    # surface only: supplied signed claims are validated without collection,
    # build, delivery, replay, runtime, or live-effect authority.
    "node_rsd_v4_artifact_evidence_validate_compute",
    # The V5 worker-evidence closure validator is likewise inventory-only.
    "node_rsd_v5_artifact_evidence_validate_compute",
    # B1 keeps its historical-map comparison private to offline evidence
    # verification; the inventory surface has no live route or entry point.
    "node_rsd_b1_projection_binding_validate_compute",
    # B2 only revalidates supplied signed evidence; it has no live route.
    "node_rsd_target_delivery_artifact_manifest_v2_validate_compute",
    # OMN-19399: the worktree-reconcile effect reads the host's own filesystem,
    # so a host timer runs it from the command line; it publishes events but
    # subscribes to no topic and has no onex.nodes entry point.
    "node_worktree_reconcile_effect",
    # The host-reconcile effect is likewise run from the command line by a host
    # timer; it subscribes to no topic and has no onex.nodes entry point.
    "node_host_reconcile_effect",
    # OMN-20712: hosted from the operator tooling repository, whose copy is
    # still registered; called in process by its host scheduler, so it subscribes
    # to no topic and has no onex.nodes entry point until the registration
    # hand-over (nodes-to-market plan step B).
    "node_lab_fill_selection_compute",
    # OMN-20712: hosted from the operator tooling repository, whose copy is
    # still registered; called in process by its host scheduler, so it subscribes
    # to no topic and has no onex.nodes entry point until the registration
    # hand-over (nodes-to-market plan step B).
    "node_lab_disk_hygiene_effect",
    # OMN-20674: hosted from the operator tooling repository, whose copy is
    # still registered; its briefs, paths and lanes arrive through a deployment
    # overlay, so it has no onex.nodes entry point until the registration
    # hand-over (nodes-to-market plan step B).
    "node_morning_friction_sweep_orchestrator",
    # OMN-19970: the dev seed runs from `onex seed` against the store or broker
    # it names; it publishes fixture terminals but subscribes to no topic and
    # has no onex.nodes entry point.
    "node_dev_seed_effect",
    # OMN-19985: the local secret store runs from `onex secret set` / `onex
    # secret delete`; it returns credential events for the CLI shim to fold and
    # subscribes to no topic, so it has no onex.nodes entry point.
    "node_local_secret_store_effect",
    # OMN-20926: the captured secret resolver is called in-process by a
    # consumer that holds a captured record, with a store built from the
    # consumer's own identity; it returns the value as a SecretStr, publishes
    # nothing and subscribes to no topic, so it has no onex.nodes entry point.
    "node_captured_secret_resolve_effect",
    # OMN-20817: the model setup effect runs from `onex models`; it returns its
    # status and test results to the CLI shim and subscribes to no topic, so it
    # has no onex.nodes entry point.
    "node_model_setup_effect",
}

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
    # OMN-14648's report-only projection is non-addressable: 4 -> 5.
    # OMN-19824/OMN-19829's node_pr_landing_reducer and
    # node_pr_landing_orchestrator are experimental-lifecycle wave-1 seams
    # with no handler_routing yet (staged landing; see each node's
    # contract.yaml header), so build_report's experimental_handler_pending bucket skips
    # them rather than counting them as failed: 5 -> 7. The wave-3 compose
    # (OMN-19829) wires the orchestrator, so only the reducer, which the
    # orchestrator calls in process, stays experimental: 7 -> 6.
    # OMN-19976's node_local_dashboard_serve_effect is started by
    # `onex dashboard`, not by a bus command, so it is experimental with no
    # handler_routing and lands in the same bucket: 6 -> 7.
    # OMN-20496's node_canonical_clone_refresh_effect is hosted by one
    # `clone-refresh serve` process per host, not by a runtime, so it is
    # experimental with no handler_routing and lands there too: 7 -> 8.
    # OMN-20604's node_lab_job_reducer is called in process by the lab job
    # orchestrator, like the landing reducer, so it is experimental with no
    # handler_routing: 8 -> 9. Its node_lab_job_submit_effect is published to
    # by the submit CLI and has no handler_routing either: 9 -> 10.
    # node_prune_binding_effect is called in process by the two prune effects,
    # so it is experimental with no handler_routing: 10 -> 11.
    # The manifest-fetch canary is invoked in process and has no bus route: 11 -> 12.
    # The five NL-to-ticket nodes are invoked in process and have no bus route: 12 -> 17.
    # OMN-20474's node_projection_delegation_judged_acceptance is a pure fold
    # the writer calls in process, so it has no handler_routing: 17 -> 18.
    # node_work_ledger_delegation_mirror is hosted by the single ledger serve
    # process, not by a runtime, so it has no handler_routing: 18 -> 19.
    assert summary["skipped"] == 19
    assert summary["failed"] == 0
    assert summary["failure_buckets"] == {}
    assert {
        item["node_name"]
        for item in report["skipped"]
        if item["bucket"] == "non_addressable"
    } == {
        "node_e2e_orchestrator",
        "node_merge_state_projection",
        "node_navigation_history_reducer",
        "node_projection_dep_health",
        "node_pr_merged_projection",  # OMN-13226 T2 stub; handler pending T3
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

    command_addressable_nodes = FOCUS_NODES - NATIVE_NON_ADDRESSABLE_NODES
    assert command_addressable_nodes <= set(routable)
    assert all(
        routable[node_name]["command_topic"].startswith("onex.cmd.")
        for node_name in command_addressable_nodes
    )
