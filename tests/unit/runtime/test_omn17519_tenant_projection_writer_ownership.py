# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant-domain projections must be owned by their dedicated writer (OMN-17519).

Background
----------
onex-dev deploy run 33638367122 (2026-09-02) failed rollout with
``omninode-runtime`` and ``omninode-runtime-effects`` in CrashLoopBackOff. The
verbatim boot error, from ``kubectl logs deploy/omninode-runtime``::

    omnibase_core.errors.model_onex_error.ModelOnexError: Auto-wiring failed
    for 7 contract(s): ... projection_pattern_learning: ... handler=
    HandlerProjectionPatternLearning: ValueError: Projection handler requires
    topology bindings with configured DSNs: tenant_projection:ONEX_TENANT_DB_URL

raised at ``handler_wiring.py`` ``_make_projection_dispatch_callback`` under
``ONEX_WIRING_STRICT_MODE=1``.

Two of those seven are the subject of this harness:
``projection_pattern_learning`` and ``projection_routing_decision``. Both are
the OMN-15905 dedicated-writer shape -- a standalone ``*ProjectionRunner``
sibling that a writer Deployment runs as its own process
(``python -m ...handler_pattern_learning``), plus a legacy in-process
projection handler. On ``projection_savings`` (5 subscribe topics) and
``projection_delegation`` (9) the runner takes every topic and the in-process
sibling is left with none, so OMN-17519's zero-route predicate correctly
declines to open a database for it.

These two contracts declare exactly ONE subscribe topic, and
``_topics_for_handler_entry`` short-circuits ``if len(topics) == 1: return
topics`` BEFORE the multi-handler ambiguity guard. Both entries therefore
receive the same topic: the runner no-ops (OMN-16874 standalone-runner branch)
AND the in-process handler is dispatched, so the shared runtime resolves the
tenant-domain workload DSN for rows the writer owns. The credential is bound on
no onex-dev pod, and strict mode makes the miss fatal for the whole boot.

The invariant
-------------
Operator ruling (2026-09-02, OMN-17519): *projections that need the
tenant-domain database are owned by dedicated writer pods, not the shared
runtime.* The topology encodes the same thing independently -- in
``omnibase_infra/topology/instances/onex-dev.yaml`` every one of these tables is
granted to principal ``tenant_projection_writer`` only, and the shared
runtime's own principal ``omninode_runtime`` holds no grant on them, so the
database would refuse the write even if a DSN were bound.

So: **a contract whose projection tables resolve to the TENANT domain and which
ships a standalone runner must route its subscribe topics to that runner
alone.** The shared runtime then takes the OMN-16874 branch for every dispatched
entry, opens no projection database, and records the skip on the OMN-17448
ledger so the no-op is visible instead of silent.

Deliberately NOT asserted here: the same double-dispatch shape exists on
``projection_baselines``, ``projection_intent_classification`` and
``projection_session_outcome``. Their relations are ``omninode_internal``, not
``tenant``; the shared runtime's DSN for that domain IS bound, no dedicated
writer Deployment exists for them, and their in-process handler is the only
process writing those rows today. Exempting them would stop the writes. The
domain is the line, and it is derived from the topology below rather than
hardcoded.

References
----------
OMN-17519 (this repair), OMN-15905 (the dedicated-writer pattern), OMN-16874
(``_is_standalone_projection_runner``), OMN-15655 / ADR-0027 (relation domain
classification).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Final

import pytest
import yaml
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain
from omnibase_core.models.container.model_onex_container import ModelONEXContainer
from omnibase_core.models.contracts.subcontracts.model_db_table_declaration import (
    ModelDbTableDeclaration,
)
from omnibase_infra.runtime.auto_wiring import handler_wiring as _handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _topics_for_handler_entry,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    filter_manifest_for_runtime_profile,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

pytestmark = pytest.mark.unit

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_NODES_DIR: Final[Path] = _REPO_ROOT / "src" / "omnimarket" / "nodes"

# The lane whose boot this reproduces. Its manifests are the ones the failing
# pods run.
_TOPOLOGY_PROFILE: Final[str] = "onex-dev"

# The two runtime profiles the crash-looping Deployments boot with
# (k8s/onex-dev/runtime/deployment-omninode-runtime{,-effects}.yaml).
_RUNTIME_PROFILES: Final[tuple[str, ...]] = ("main", "effects")

# A handler entry whose class name carries this suffix is the standalone
# writer-process entrypoint. The runtime's own predicate
# (``_is_standalone_projection_runner``) is capability-shaped and needs an
# INSTANCE, which a contract-level census cannot build; the suffix is how the
# contract declares the intent, and the end-to-end wiring test below proves the
# runtime agrees.
_RUNNER_SUFFIXES: Final[tuple[str, ...]] = ("ProjectionRunner", "ProjectionWriter")


def _contract_yaml_paths() -> tuple[Path, ...]:
    return tuple(sorted(_NODES_DIR.glob("*/contract.yaml")))


def _tenant_domain_contracts_with_a_standalone_runner() -> tuple[str, ...]:
    """Node directories whose projection relations are TENANT and ship a runner.

    Derived from the checked-in contracts and the checked-in topology, never
    hardcoded: a new tenant-domain projection that grows a runner sibling joins
    this census automatically.
    """
    topology = load_topology_profile(_TOPOLOGY_PROFILE)
    selected: list[str] = []
    for path in _contract_yaml_paths():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        routing = raw.get("handler_routing") or {}
        entries = routing.get("handlers") or []
        # Deliberately no minimum entry count. A contract that has already been
        # reduced to its runner alone must STAY in the census -- otherwise the
        # ratchet releases the moment it is repaired and a later edit could
        # re-add an in-process sibling with nothing to catch it.
        has_runner = any(
            str(((entry.get("handler") or {}).get("name")) or "").endswith(
                _RUNNER_SUFFIXES
            )
            for entry in entries
        )
        if not has_runner:
            continue
        tables_raw = (raw.get("db_io") or {}).get("db_tables") or []
        if not tables_raw:
            continue
        domains = set()
        for table_raw in tables_raw:
            table = ModelDbTableDeclaration.model_validate(table_raw)
            domains.add(topology.schema_domain(table.database_ref, table.schema))
        if domains == {EnumDatabaseSchemaDomain.TENANT}:
            selected.append(path.parent.name)
    return tuple(selected)


_TENANT_WRITER_OWNED: Final[tuple[str, ...]] = (
    _tenant_domain_contracts_with_a_standalone_runner()
)


def test_the_census_is_not_empty() -> None:
    """Guard the guard: an empty census would make every assertion vacuous."""
    assert _TENANT_WRITER_OWNED, (
        "no omnimarket contract pairs TENANT-domain projection relations with a "
        "standalone runner entry; if that is genuinely true this harness is "
        "obsolete, but far more likely the derivation above stopped matching"
    )


@pytest.mark.parametrize("node_dir", _TENANT_WRITER_OWNED)
def test_only_the_standalone_runner_is_assigned_subscribe_topics(
    node_dir: str,
) -> None:
    """The in-process sibling must be assigned NO topic in the shared runtime.

    RED before the repair: ``_topics_for_handler_entry`` hands the sole
    subscribe topic to BOTH entries, so the in-process projection handler is
    dispatched here and resolves ``tenant_projection:ONEX_TENANT_DB_URL``.
    """
    manifest = discover_contracts()
    contract = next(
        (c for c in manifest.contracts if c.contract_path.parent.name == node_dir),
        None,
    )
    assert contract is not None, f"{node_dir} was not discovered"
    assert contract.handler_routing is not None

    assigned = {
        entry.handler.name: _topics_for_handler_entry(contract, entry)
        for entry in contract.handler_routing.handlers
    }
    dispatched = sorted(name for name, topics in assigned.items() if topics)
    in_process = [name for name in dispatched if not name.endswith(_RUNNER_SUFFIXES)]

    assert not in_process, (
        f"{node_dir}: the shared runtime dispatches {in_process} for a "
        f"TENANT-domain projection. Every dispatched entry must be the "
        f"standalone writer entry, or this process resolves "
        f"tenant_projection:ONEX_TENANT_DB_URL for rows the dedicated writer "
        f"Deployment owns -- a credential bound on no onex-dev pod, which "
        f"under ONEX_WIRING_STRICT_MODE=1 takes the whole boot down. "
        f"Assignments: {assigned}"
    )


# The end-to-end assertion below covers the WHOLE census, and the rest of the
# census (``projection_savings``, ``projection_registration``,
# ``projection_tenant_credentials``, ``projection_live_events``) is kept clean
# by the zero-route exemption that shipped in omnibase_infra #3136. This repo's
# floor pin (omnibase-infra>=0.38.15) predates it, so on an older resolved
# omnibase_infra the end-to-end run is red for reasons no omnimarket change can
# fix. Detect that directly rather than asserting against a runtime that cannot
# satisfy the invariant; the check re-arms by itself when the pin advances.
_ZERO_ROUTE_EXEMPTION_PRESENT: Final[bool] = hasattr(
    _handler_wiring, "_projection_dispatch_owned_elsewhere"
)


@pytest.mark.skipif(
    not _ZERO_ROUTE_EXEMPTION_PRESENT,
    reason=(
        "resolved omnibase_infra predates OMN-17519 (#3136): "
        "_projection_dispatch_owned_elsewhere is absent, so the writer-owned "
        "zero-route entries this census also covers cannot wire clean here"
    ),
)
@pytest.mark.parametrize("runtime_profile", _RUNTIME_PROFILES)
def test_shared_runtime_wires_tenant_projections_without_a_tenant_dsn(
    runtime_profile: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the exact boot path, with the exact onex-dev pod environment.

    Reproduces ``service_kernel.bootstrap -> wire_from_manifest`` for one
    runtime profile with the DSNs the pods actually bind
    (``k8s/onex-dev/runtime/deployment-omninode-runtime{,-effects}.yaml``) and,
    critically, WITHOUT ``ONEX_TENANT_DB_URL`` -- which is bound on no onex-dev
    pod. RED before the repair with the deploy's verbatim message.
    """
    for key in (
        "OMNIBASE_INFRA_DB_URL",
        "OMNINODE_INTERNAL_DB_URL",
        "OMNIDASH_ANALYTICS_DB_URL",
    ):
        monkeypatch.setenv(key, "postgresql://probe:probe@127.0.0.1:5432/omn17519")
    # The effects manifest literally sets `value: ""` for this one.
    monkeypatch.setenv("OMNIINTELLIGENCE_DB_URL", "")
    monkeypatch.delenv("ONEX_TENANT_DB_URL", raising=False)
    monkeypatch.setenv("ONEX_ENVIRONMENT", "dev")
    # k8s/onex-dev/runtime/configmap.yaml
    monkeypatch.setenv("ONEX_ACTIVE_RUNTIME_PACKAGES", "omnibase_infra,omnimarket")
    # Collect every failure instead of raising on the first one, so a
    # regression names all of them at once.
    monkeypatch.setenv("ONEX_WIRING_STRICT_MODE", "0")

    manifest = discover_contracts()
    owned = filter_manifest_for_runtime_profile(manifest, runtime_profile).manifest
    subset = tuple(
        c
        for c in owned.contracts
        if c.contract_path.parent.name in _TENANT_WRITER_OWNED
    )
    if not subset:
        pytest.skip(f"no census contract is owned by the {runtime_profile!r} profile")

    report = asyncio.run(
        wire_from_manifest(
            manifest=ModelAutoWiringManifest(contracts=subset),
            dispatch_engine=MessageDispatchEngine(),
            event_bus=_StubEventBus(),
            container=ModelONEXContainer(),
            subscribe_immediately=False,
            topology=load_topology_profile(_TOPOLOGY_PROFILE),
        )
    )

    failures = {
        result.contract_name: result.reason
        for result in report.results
        if str(result.outcome).endswith("FAILED")
    }
    assert not failures, (
        f"{runtime_profile!r} profile: {len(failures)} TENANT-domain projection "
        f"contract(s) still resolve a workload DSN in the shared runtime: "
        f"{failures}"
    )


class _StubEventBus:
    """Two-method stand-in for the pod's Kafka bus.

    Only ``publish`` is probed at wiring time; passing ``None`` instead makes
    every handler that declares ``event_publisher`` fail for a reason the live
    pod never has.
    """

    async def publish(self, *args: object, **kwargs: object) -> None:
        return None

    async def subscribe(self, *args: object, **kwargs: object) -> None:
        return None
