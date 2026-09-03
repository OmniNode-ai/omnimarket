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

# OMN-17641 INTERIM -- this constant and every use of it is deleted by the
# OMN-17556 PR. Eight tenant-domain projections were moved onto the
# ``tenant-projection`` profile, which no process boots today, precisely so the
# shared runtime stops resolving a DSN it cannot have. Two of the eight are
# REDUCER_GENERIC, so the invariant below is genuinely violated by them: they
# start no consumer group and materialize nothing. That is the intended,
# ticketed state, not the silent OMN-12950 accident this test exists to catch.
#
# The exemption is READ FROM the audited allowlist rather than hardcoded here,
# so there is exactly one place to delete: when OMN-17556 lands the consolidated
# writer and removes the interim allowlist block, this set becomes empty and the
# guard tightens back to fail-closed with no edit to this file.
_INTERIM_TICKET = "OMN-17641"
_ALLOWLIST_PATH = REPO_ROOT / "validation" / "runtime_profiles_allowlist.yaml"


def _interim_orphaned_node_ids() -> frozenset[str]:
    """Contract names the OMN-17641 interim block deliberately orphans."""
    raw = yaml.safe_load(_ALLOWLIST_PATH.read_text(encoding="utf-8")) or {}
    entries = raw.get("allowlist") or []
    return frozenset(
        str(entry["node_id"])
        for entry in entries
        if _INTERIM_TICKET in str(entry.get("reason", ""))
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

    # OMN-17641 INTERIM (deleted by the OMN-17556 PR): a reducer named in the
    # interim allowlist block is deliberately orphaned onto ``tenant-projection``
    # until the consolidated writer boots that profile. Assert the deliberate
    # shape instead of the deployed-lane invariant -- the orphaning is checked,
    # not waived.
    if str(raw.get("name")) in _interim_orphaned_node_ids():
        assert declared == ("tenant-projection",), (
            f"{contract_path.parent.name} carries an {_INTERIM_TICKET} allowlist "
            f"exemption but declares runtime_profiles={declared}; the exemption "
            "only covers the interim tenant-projection move."
        )
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
