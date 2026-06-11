# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""REDUCER_GENERIC contracts must be owned by a deployed standalone-consumer runtime.

A reducer runs as a standalone bus consumer: the runtime that owns its contract
starts a consumer group and subscribes to ``event_bus.subscribe_topics``. Ownership
is decided by ``omnibase_infra`` auto-wiring: a contract whose ``runtime_profiles``
names a profile that no running runtime advertises is wired by nobody, so no
consumer group is ever created and its terminal/projection events never fire.

OMN-12950: ``node_deployment_evidence_reducer`` and ``node_evidence_dashboard_reducer``
declared ``runtime_profiles: [compute]``. ``compute`` is not a registered runtime
profile (see ``omnibase_infra.runtime.runtime_profile._PROFILES``) and no deployed
runtime runs ``RUNTIME_PROFILE=compute``, so both reducers silently never consumed
their subscriptions and the five evidence/readiness projection tables stayed empty
even though the upstream handlers succeeded and emitted their terminal events.

These tests assert the fix and pin the invariant against recurrence:

1. Both evidence reducers are owned by the ``workers`` runtime.
2. Every REDUCER_GENERIC contract is owned by at least one deployed
   standalone-consumer runtime profile.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    extract_runtime_profiles_from_contract,
    runtime_profile_owns_contract,
)
from omnibase_infra.runtime.runtime_profile import _PROFILES

REPO_ROOT = Path(__file__).resolve().parents[2]
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

# Runtime profiles that, on every deployed lane (dev / stability-test / prod),
# run a standalone runtime process which starts consumer groups for the
# contracts it owns. A reducer MUST be owned by one of these or it never
# consumes anything. ``main``/``effects``/``workers`` are the three runtime
# containers that boot on the stability lane (verified 2026-06-11:
# RUNTIME_PROFILE=main/effects/workers); a reducer with no ``runtime_profiles``
# defaults to ``main`` ownership and is therefore also covered.
_DEPLOYED_STANDALONE_CONSUMER_PROFILES: frozenset[str] = frozenset(
    {"main", "effects", "workers"}
)

_EVIDENCE_REDUCERS = (
    "node_deployment_evidence_reducer",
    "node_evidence_dashboard_reducer",
)


def _load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert isinstance(raw, dict)
    return raw


def _reducer_contract_paths() -> tuple[Any, ...]:
    cases: list[Any] = []
    for contract_path in sorted(NODES_ROOT.glob("*/contract.yaml")):
        raw = _load_contract(contract_path)
        if raw.get("node_type") != "REDUCER_GENERIC":
            continue
        cases.append(pytest.param(contract_path, id=contract_path.parent.name))
    return tuple(cases)


def test_deployed_standalone_consumer_profiles_are_registered() -> None:
    """Guard against drift: the lanes we trust must be real runtime profiles."""
    registered = set(_PROFILES)
    missing = _DEPLOYED_STANDALONE_CONSUMER_PROFILES - registered
    assert not missing, (
        f"deployed-consumer profiles not registered in _PROFILES: {sorted(missing)}; "
        f"registered: {sorted(registered)}"
    )


@pytest.mark.parametrize("node_name", _EVIDENCE_REDUCERS)
def test_evidence_reducers_owned_by_workers_runtime(node_name: str) -> None:
    """OMN-12950: both evidence reducers must be owned by the ``workers`` runtime."""
    raw = _load_contract(NODES_ROOT / node_name / "contract.yaml")
    assert raw.get("node_type") == "REDUCER_GENERIC"

    # Resolve ownership through the SAME function the runtime uses at wiring time.
    assert runtime_profile_owns_contract(raw, "workers") is True, (
        f"{node_name} is not owned by the 'workers' runtime; "
        f"declared runtime_profiles={extract_runtime_profiles_from_contract(raw)}"
    )
    # It must NOT name the bogus 'compute' profile that caused the regression.
    assert "compute" not in extract_runtime_profiles_from_contract(raw)


@pytest.mark.parametrize("contract_path", _reducer_contract_paths())
def test_every_reducer_owned_by_a_deployed_consumer_runtime(
    contract_path: Path,
) -> None:
    """No REDUCER_GENERIC contract may be stranded off every deployed consumer lane.

    A reducer owned only by an unregistered or non-standalone profile starts no
    consumer group and silently materializes nothing — the OMN-12950 failure mode.
    """
    raw = _load_contract(contract_path)

    declared = extract_runtime_profiles_from_contract(raw)
    # Unscoped reducers default to ``main`` ownership, which is a deployed lane.
    if not declared:
        assert runtime_profile_owns_contract(raw, "main") is True
        return

    owning_profiles = [
        profile
        for profile in _DEPLOYED_STANDALONE_CONSUMER_PROFILES
        if runtime_profile_owns_contract(raw, profile)
    ]
    assert owning_profiles, (
        f"{contract_path.parent.name} declares runtime_profiles={declared}, none of "
        f"which is a deployed standalone-consumer runtime "
        f"{sorted(_DEPLOYED_STANDALONE_CONSUMER_PROFILES)}; the reducer would start "
        f"no consumer group and materialize nothing (OMN-12950 failure mode)."
    )
