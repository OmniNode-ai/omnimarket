# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every omnimarket node wires into the infra runtime before it ships (OMN-17427).

On 2026-10-09 omnimarket 0.4.308 shipped two nodes,
``node_delegation_acceptance_judged_replay_compute`` and
``node_delegation_acceptance_judged_publish_effect``, whose auto-wired route
ids were longer than ``ModelDispatchRoute.route_id`` allows. The .201 dev lane
runs ``ONEX_WIRING_STRICT_MODE=1``, so the ``ValidationError`` killed boot and
both runtimes crash-looped. Nothing on the omnimarket side had ever put its
contracts through the infra runtime's auto-wiring: the CI runtime boots ran
non-strict and quarantined the failure, so they stayed green.

Two checks close that gap, both against the omnibase-infra this repo resolves
(the runtime a release of this package declares it runs on):

1. Every dispatch identifier the runtime derives for every handler entry of
   every contract (dispatcher id, route id, topic pattern) fits the limit
   ``ModelDispatchRoute`` declares for its field. The derivation is infra's
   own (``_derive_route_id`` and its siblings, the functions
   ``_prepare_handler_wiring`` calls) and every limit is read from the model's
   field metadata, so neither side is restated here.
2. Every contract wires in-process through infra's real ``wire_from_manifest``
   with a real DI container, the checked-in ``local`` deployment topology, an
   unreachable placeholder DSN for each binding that topology declares, and
   an in-memory bus. A contract that fails, or whose handler is quarantined as
   unresolvable (the failures ``ONEX_WIRING_STRICT_MODE`` turns into a boot
   crash), fails this test.

Both lists of known exceptions below are shrink-only. A contract not on its
list that fails is a new defect; a listed contract that now passes fails the
test until it is removed from the list in the same change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.container import ModelONEXContainer
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _derive_dispatcher_id,
    _derive_handler_entry_key,
    _derive_route_id,
    _derive_topic_pattern_from_topic,
    _topics_for_handler_entry,
    wire_from_manifest,
)
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.report import (
    _RESOLUTION_FAILURE_REASONS,
    EnumWiringOutcome,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_NODES = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_THIS_TEST = "tests/unit/contracts/test_every_node_auto_wires_in_process_omn17427.py"
_PRECOMMIT_HOOK_ID = "node-auto-wiring-in-process"

# A DSN no handler can reach: port 9 is the discard port. Construction must not
# need a live database; a handler that connects at construction fails here, as
# it would on a lane whose database is still starting.
_UNREACHABLE_DSN = "postgresql://wiring-check:wiring-check@127.0.0.1:9/wiring_check"
# Every runtime is started with a broker address and a database-topology
# profile; this test supplies both, the broker unreachable and the profile the
# one whose bindings it loads.
_UNREACHABLE_BROKER = "127.0.0.1:9"
_TOPOLOGY_PROFILE = "local"

# Shrink-only. Contracts whose derived dispatch identifiers exceed a
# ModelDispatchRoute limit under the omnibase-infra this repo resolves.
_IDS_OVER_LIMIT: dict[str, str] = {
    "node_delegation_acceptance_judged_publish_effect": (
        "route.auto.<contract>.<handler>.<topic> is 206 characters; the 0.38.69 "
        "runtime does not bound derived ids. omnibase_infra#4767 bounds them with "
        "a digest suffix; remove this entry when the omnibase-infra pin carries it"
    ),
    "node_delegation_acceptance_judged_replay_compute": (
        "same derived route id class as the publish effect; same exit condition"
    ),
}

# Shrink-only. Contracts that cannot wire in-process for a reason this test
# cannot supply honestly (a secret, an operator overlay, explicit dependencies)
# or because of a defect already filed.
_CANNOT_WIRE_IN_PROCESS: dict[str, str] = {
    "node_delegation_acceptance_judged_publish_effect": (
        "ModelDispatchRoute ValidationError on the derived route id (see _IDS_OVER_LIMIT)"
    ),
    "node_delegation_acceptance_judged_replay_compute": (
        "ModelDispatchRoute ValidationError on the derived route id (see _IDS_OVER_LIMIT)"
    ),
    "node_handshake_policy_gate_effect": (
        "the routing authority declares secret GH_TOKEN, resolved from the store on a lane"
    ),
    "node_adr_canary_orchestrator": (
        "HandlerCanaryOrchestrator needs the ADR bus protocol as an explicit dependency"
    ),
    "node_memory_storage_effect": (
        "omnimemory HandlerFileSystemAdapter requires a config constructor argument"
    ),
}

# The ModelDispatchRoute field each derived identifier is stored in.
_DERIVED_FIELDS = ("route_id", "handler_id", "topic_pattern")


def _field_max_length(field_name: str) -> int:
    """The max_length ModelDispatchRoute declares for one field, read from the model."""
    for constraint in ModelDispatchRoute.model_fields[field_name].metadata:
        max_length = getattr(constraint, "max_length", None)
        if isinstance(max_length, int):
            return max_length
    raise AssertionError(f"ModelDispatchRoute.{field_name} declares no max_length")


def _manifest() -> ModelAutoWiringManifest:
    discovered = discover_contracts_from_paths(sorted(_NODES.glob("*/contract.yaml")))
    assert not discovered.errors, discovered.errors
    assert discovered.contracts, f"no contracts discovered under {_NODES}"
    return ModelAutoWiringManifest(contracts=tuple(discovered.contracts))


def _shrink_only(actual: dict[str, str], listed: dict[str, str], what: str) -> None:
    new = sorted(set(actual) - set(listed))
    fixed = sorted(set(listed) - set(actual))
    assert not new, f"{what}:\n" + "\n".join(
        f"  {name}: {actual[name]}" for name in new
    )
    assert not fixed, (
        f"listed as known but now pass; remove them from the list in this change: {fixed}"
    )


def _gate_step_problems(workflow_path: str, *, before_build: bool) -> list[str]:
    """Problems with the workflow steps that run this test as a blocking gate."""
    workflow: Any = yaml.safe_load((_REPO_ROOT / workflow_path).read_text())
    problems: list[str] = []
    found = False
    for job in (workflow.get("jobs") or {}).values():
        steps = job.get("steps") or []
        runs = [str(step.get("run", "")) for step in steps]
        gates = [i for i, run in enumerate(runs) if _THIS_TEST in run]
        if not gates:
            continue
        found = True
        for i in gates:
            step = steps[i]
            if step.get("continue-on-error") in (True, "true"):
                problems.append(
                    f"{workflow_path}: {step.get('name')!r} is continue-on-error"
                )
            if step.get("if") is not None:
                problems.append(f"{workflow_path}: {step.get('name')!r} is conditional")
        if before_build:
            builds = [i for i, run in enumerate(runs) if "uv build" in run]
            if builds and min(gates) > min(builds):
                problems.append(f"{workflow_path}: the gate runs after uv build")
    if not found:
        problems.append(f"{workflow_path}: no step runs {_THIS_TEST}")
    return problems


def test_the_gate_runs_on_every_pull_request_commit_and_release() -> None:
    problems = _gate_step_problems(".github/workflows/ci.yml", before_build=False)
    for release in (
        ".github/workflows/release.yml",
        ".github/workflows/release-cut.yml",
    ):
        problems += _gate_step_problems(release, before_build=True)
    config: Any = yaml.safe_load((_REPO_ROOT / ".pre-commit-config.yaml").read_text())
    hooks = [
        hook
        for repo in config.get("repos") or []
        for hook in repo.get("hooks") or []
        if hook.get("id") == _PRECOMMIT_HOOK_ID
    ]
    if not any(_THIS_TEST in str(hook.get("entry", "")) for hook in hooks):
        problems.append(
            f".pre-commit-config.yaml: no {_PRECOMMIT_HOOK_ID} hook runs {_THIS_TEST}"
        )
    assert not problems, "\n".join(problems)


def test_limits_are_read_from_the_dispatch_route_model() -> None:
    for field_name in _DERIVED_FIELDS:
        assert _field_max_length(field_name) > 0


def test_every_derived_dispatch_identifier_fits_the_runtime_limits() -> None:
    limits = {name: _field_max_length(name) for name in _DERIVED_FIELDS}
    over: dict[str, str] = {}
    for contract in _manifest().contracts:
        if contract.handler_routing is None or contract.event_bus is None:
            continue
        for entry in contract.handler_routing.handlers:
            handler_key = _derive_handler_entry_key(entry)
            derived = [
                ("handler_id", _derive_dispatcher_id(contract.name, handler_key))
            ]
            for topic in _topics_for_handler_entry(contract, entry):
                derived.append(
                    ("route_id", _derive_route_id(contract.name, handler_key, topic))
                )
                derived.append(
                    ("topic_pattern", _derive_topic_pattern_from_topic(topic))
                )
            for field_name, value in derived:
                if len(value) > limits[field_name]:
                    over.setdefault(
                        contract.name,
                        f"{field_name} is {len(value)} characters, limit "
                        f"{limits[field_name]}: {value}",
                    )
    _shrink_only(
        over,
        _IDS_OVER_LIMIT,
        "derived dispatch identifiers exceed the ModelDispatchRoute limit; the "
        "runtime refuses the route at boot (ONEX_WIRING_STRICT_MODE crashes it)",
    )


async def test_every_node_auto_wires_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topology = load_topology_profile(_TOPOLOGY_PROFILE)
    monkeypatch.setenv("ONEX_DATABASE_TOPOLOGY_PROFILE", _TOPOLOGY_PROFILE)
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", _UNREACHABLE_BROKER)
    for database in topology.databases.values():
        for binding in database.bindings.values():
            if binding.dsn_env:
                monkeypatch.setenv(binding.dsn_env, _UNREACHABLE_DSN)
    monkeypatch.delenv("ONEX_WIRING_STRICT_MODE", raising=False)

    bus = EventBusInmemory()
    try:
        report = await wire_from_manifest(
            _manifest(),
            MessageDispatchEngine(),
            event_bus=bus,
            environment=_TOPOLOGY_PROFILE,
            container=ModelONEXContainer(),
            topology=topology,
        )
    finally:
        await bus.close()

    failing: dict[str, str] = {}
    for result in report.results:
        if result.outcome == EnumWiringOutcome.FAILED:
            failing[result.contract_name] = str(result.reason)[:400]
    for quarantined in report.quarantined_handlers:
        if quarantined.reason in _RESOLUTION_FAILURE_REASONS:
            failing.setdefault(
                quarantined.contract_name,
                f"{quarantined.handler_name}: {quarantined.detail[:400]}",
            )
    assert report.total_wired > 0
    _shrink_only(
        failing,
        _CANNOT_WIRE_IN_PROCESS,
        "contracts that fail infra auto-wiring in-process (a crash loop under "
        "ONEX_WIRING_STRICT_MODE=1, the dev lane's setting)",
    )
