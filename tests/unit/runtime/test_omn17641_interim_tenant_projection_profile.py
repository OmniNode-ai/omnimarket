# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17641 -- INTERIM. This whole file is deleted by the OMN-17556 PR.

What this guards
----------------
Eight omnimarket projection contracts write TENANT-domain tables, so their
handlers resolve the ``tenant_projection`` binding, whose DSN
(``ONEX_TENANT_DB_URL``) no onex-dev pod supplies and -- per the 2026-09-03
operator ruling -- never will as a container env var. Under
``ONEX_WIRING_STRICT_MODE=1`` a single unresolvable binding is fatal, so all
eight together held ``omninode-runtime``, ``omninode-runtime-effects`` and
``omninode-runtime-worker`` in CrashLoopBackOff, and took
``omnimarket-projection-api`` down with them (the boot-side per-contract topic
provisioner runs *after* the strict raise, so
``onex.snapshot.projection.consumer-flow.v1`` was never created).

The interim change is profile membership and nothing else: each of the eight
declares ``runtime_profiles: [tenant-projection]``, a profile no process boots
today, so ``filter_manifest_for_runtime_profile`` drops it from ``main`` and
``effects``. No handler was touched, no env var was added, and the projection
DSN requirement was not loosened by one inch. While this stands the eight
consume nothing and write nothing -- deliberate, ticketed orphaning, which is
strictly better than the prior state where they also wrote nothing AND took the
runtime plane down.

Reverting change: OMN-17556 (RATIFIED) boots one consolidated writer with
``RUNTIME_PROFILE=tenant-projection`` and the credential resolved from the
store at the binding boundary. That PR deletes this file, deletes the eight
``validation/runtime_profiles_allowlist.yaml`` entries, and registers
``tenant-projection`` in ``REGISTERED_RUNTIME_PROFILES``.

Red baseline, measured 2026-09-03 on omnimarket ``dev`` (c85362d1) with
omnibase_infra ``dev`` (77ffa91c) and strict mode OFF so every failure is
collected instead of the first one raising: ``main`` reported 7 auto-wiring
failures and ``effects`` 1 -- exactly the eight named below, each
``Projection handler requires topology bindings with configured DSNs:
tenant_projection:ONEX_TENANT_DB_URL``. With this change and strict mode ON,
both profiles wire clean and nothing raises.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest
import yaml
from omnibase_core.errors.model_onex_error import ModelOnexError
from omnibase_core.models.container.model_onex_container import ModelONEXContainer
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.auto_wiring.profile_ownership import (
    filter_manifest_for_runtime_profile,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.topology import load_topology_profile

# Ticket that made the interim move, and the ticket whose PR reverts it. Both
# strings are asserted against the contracts and the allowlist below, so the
# revert cannot be forgotten: deleting OMN-17556's writer work without deleting
# these entries leaves a test naming a ticket that no longer means anything.
INTERIM_TICKET: Final[str] = "OMN-17641"
REVERTING_TICKET: Final[str] = "OMN-17556"

INTERIM_PROFILE: Final[str] = "tenant-projection"

# contract `name:` -> node directory. Seven were on `main` (they declared no
# runtime_profiles at all, and unscoped contracts default to `main`);
# node_hook_event_capture explicitly declared `[effects]`.
INTERIM_REMOVED_CONTRACTS: Final[dict[str, str]] = {
    "canary_score_reducer": "node_canary_score_reducer",
    "projection_context_roi": "node_projection_context_roi",
    "node_projection_cost_summary": "node_projection_cost_summary",
    "node_projection_delegation_inference_response": (
        "node_projection_delegation_inference_response"
    ),
    "node_projection_dep_health": "node_projection_dep_health",
    "projection_pattern_learning": "node_projection_pattern_learning",
    "projection_routing_decision": "node_projection_routing_decision",
    "node_hook_event_capture": "node_hook_event_capture",
}

# tests/unit/runtime/<this file> -> parents[3] == repo root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_NODES_DIR: Final[Path] = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_ALLOWLIST_PATH: Final[Path] = (
    _REPO_ROOT / "validation" / "runtime_profiles_allowlist.yaml"
)

# The onex-dev pods' real environment, read out of
# omninode_infra/k8s/onex-dev/runtime/deployment-omninode-runtime{,-effects}.yaml
# and configmap.yaml:80. The three bound DSNs are never dialled at wiring time
# (auto-wiring resolves the binding, it does not connect), so a syntactically
# valid loopback URL is sufficient and no database is required.
_PROBE_DSN: Final[str] = "postgresql://probe:probe@127.0.0.1:5432/omn17641"
_ONEX_DEV_POD_ENV: Final[dict[str, str]] = {
    "OMNIBASE_INFRA_DB_URL": _PROBE_DSN,
    "OMNINODE_INTERNAL_DB_URL": _PROBE_DSN,
    "OMNIDASH_ANALYTICS_DB_URL": _PROBE_DSN,
    # The manifest literally sets `value: ""` for this one.
    "OMNIINTELLIGENCE_DB_URL": "",
    # consumer_flow_stall_alert_effect's handler reads this at construction and
    # raises KeyError without it. The onex-dev pod gets a real broker list from
    # its configmap; wiring never dials the broker, so a placeholder that is
    # merely present is faithful here and keeps a live endpoint out of the repo.
    "KAFKA_BOOTSTRAP_SERVERS": "kafka.invalid:9092",
    "ONEX_WIRING_STRICT_MODE": "1",
    "ONEX_ENVIRONMENT": "dev",
    "ONEX_ACTIVE_RUNTIME_PACKAGES": "omnibase_infra,omnimarket",
}

# OMN-17519's zero-route exemption (omnibase_infra#3136) ships in the runtime
# image the onex-dev plane runs but is NOT in the omnibase-infra 0.38.16 wheel
# this repo resolves from PyPI -- the OMN-16976 merged-but-unreleased gap. On a
# wheel without it, two contracts that ARE exempt on the live plane
# (projection_savings on `main`, projection_delegation on `effects`) still fail
# here. That is not this ticket's residual and this change cannot fix it, so it
# is named explicitly rather than silently tolerated. When the wheel catches up
# the branch below disappears and the assertion tightens to zero on its own.
_INFRA_HAS_ZERO_ROUTE_EXEMPTION: Final[bool] = hasattr(
    handler_wiring, "_projection_dispatch_owned_elsewhere"
)
_OMN16976_UNRELEASED_WHEEL_RESIDUAL: Final[frozenset[str]] = frozenset(
    {"projection_savings", "projection_delegation"}
)


class _StubEventBus:
    """Stand-in for the pod's Kafka bus.

    Only ``publish`` is probed at wiring time (a handler declaring
    ``event_publisher`` is rejected when the bus has no callable ``publish``).
    Passing ``event_bus=None`` instead would manufacture failures the live pod,
    which always has a real bus, never sees.
    """

    async def publish(self, *args: object, **kwargs: object) -> None:
        return None

    async def subscribe(self, *args: object, **kwargs: object) -> None:
        return None


def _contract_path(contract_name: str) -> Path:
    path = _NODES_DIR / INTERIM_REMOVED_CONTRACTS[contract_name] / "contract.yaml"
    if not path.is_file():
        raise AssertionError(
            f"contract for {contract_name!r} not found at {path} -- the "
            f"{INTERIM_TICKET} node-dir mapping is stale."
        )
    return path


def _declared_profiles(raw: dict[str, object]) -> tuple[str, ...]:
    """Mirror the canonical extractor: top-level first, then ``descriptor``."""
    profiles = raw.get("runtime_profiles")
    descriptor = raw.get("descriptor")
    if profiles is None and isinstance(descriptor, dict):
        profiles = descriptor.get("runtime_profiles")
    if profiles is None:
        return ()
    if isinstance(profiles, str):
        return (profiles,)
    if isinstance(profiles, list):
        return tuple(str(p) for p in profiles)
    raise AssertionError(
        f"runtime_profiles must be a string or list; got {type(profiles).__name__}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("contract_name", sorted(INTERIM_REMOVED_CONTRACTS))
def test_each_contract_is_interim_removed_and_names_the_reverting_ticket(
    contract_name: str,
) -> None:
    """Each of the eight declares the interim profile and cites its revert.

    Membership alone is not enough: a bare ``runtime_profiles:
    [tenant-projection]`` with no citation is indistinguishable from the
    OMN-12950 accident (a contract naming a profile no runtime boots, silently
    writing nothing forever). The citation is what makes this deliberate and
    reversible, so it is asserted, not merely encouraged.
    """
    path = _contract_path(contract_name)
    text = path.read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    assert isinstance(raw, dict), f"{path} did not parse as a mapping"
    assert raw["name"] == contract_name

    assert _declared_profiles(raw) == (INTERIM_PROFILE,), (
        f"{contract_name} must declare exactly [{INTERIM_PROFILE}] so "
        "filter_manifest_for_runtime_profile drops it from main and effects; "
        f"got {_declared_profiles(raw)!r}"
    )
    assert INTERIM_TICKET in text, (
        f"{path} declares the interim profile without citing {INTERIM_TICKET}"
    )
    assert REVERTING_TICKET in text, (
        f"{path} declares the interim profile without naming "
        f"{REVERTING_TICKET} as the ticket whose PR reverts it"
    )


@pytest.mark.unit
def test_allowlist_carries_exactly_these_eight_interim_entries() -> None:
    """The validator exemptions are per-node, reasoned, and revert-tagged.

    ``tenant-projection`` is deliberately absent from
    ``REGISTERED_RUNTIME_PROFILES``, so ``ValidatorRuntimeProfiles``'
    ``runtime_profile_unregistered`` rule fires on all eight. The rule is
    right -- they really are orphaned. The allowlist entry is the audited
    ownership statement the validator's own design provides for that case, and
    every one of them names OMN-17556 so the exemption cannot outlive the
    condition that justified it.
    """
    raw = yaml.safe_load(_ALLOWLIST_PATH.read_text(encoding="utf-8"))
    entries = raw["allowlist"]
    by_node: dict[str, str] = {e["node_id"]: e["reason"] for e in entries}

    for contract_name in INTERIM_REMOVED_CONTRACTS:
        assert contract_name in by_node, (
            f"{contract_name} declares the unregistered {INTERIM_PROFILE!r} "
            "profile but has no allowlist entry -- the required "
            "runtime_profiles CI gate will reject it."
        )
        reason = by_node[contract_name]
        assert INTERIM_TICKET in reason, (
            f"allowlist reason for {contract_name} must name the interim ticket "
            f"{INTERIM_TICKET}; got {reason!r}"
        )
        assert REVERTING_TICKET in reason, (
            f"allowlist reason for {contract_name} must name the reverting ticket "
            f"{REVERTING_TICKET}; got {reason!r}"
        )

    # Nothing else in the file may claim the interim reason: an unrelated node
    # quietly borrowing this exemption would survive the OMN-17556 cleanup.
    borrowed = {
        node
        for node, reason in by_node.items()
        if INTERIM_TICKET in reason and node not in INTERIM_REMOVED_CONTRACTS
    }
    assert not borrowed, (
        f"{sorted(borrowed)} cite {INTERIM_TICKET} but are not part of the "
        "interim set; they would be orphaned when the interim block is deleted."
    )


@pytest.mark.unit
def test_no_other_contract_claims_the_interim_profile() -> None:
    """No ninth contract may drift onto the unowned profile unnoticed."""
    claimants: set[str] = set()
    for contract_path in _NODES_DIR.rglob("contract.yaml"):
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            continue
        if INTERIM_PROFILE in _declared_profiles(raw):
            claimants.add(str(raw["name"]))
    assert claimants == set(INTERIM_REMOVED_CONTRACTS), (
        f"contracts declaring {INTERIM_PROFILE!r} drifted from the interim set: "
        f"unexpected={sorted(claimants - set(INTERIM_REMOVED_CONTRACTS))} "
        f"missing={sorted(set(INTERIM_REMOVED_CONTRACTS) - claimants)}"
    )


def _contract_names_in_strict_raise(message: str, candidates: set[str]) -> set[str]:
    """Return the contract names named by a strict-mode auto-wiring raise.

    ``wire_from_manifest`` under ``ONEX_WIRING_STRICT_MODE=1`` collects every
    failure and raises one ``ModelOnexError`` whose message is
    ``"Auto-wiring failed for N contract(s): <name>: <reason>; <name>: ..."``.
    Matching against the profile's own contract names (rather than splitting the
    message) avoids mis-parsing a reason that itself contains a colon.
    """
    return {
        name
        for name in candidates
        if re.search(rf"(?:^|[:;] ){re.escape(name)}: ", message)
    }


@pytest.mark.unit
@pytest.mark.slow
async def test_onex_dev_main_and_effects_wire_clean_under_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real onex-dev manifests wire with no failure and no raise.

    This drives the production path -- ``discover_contracts`` ->
    ``filter_manifest_for_runtime_profile`` -> ``wire_from_manifest`` -- against
    the real ``onex-dev`` topology with the pods' real environment and
    ``ONEX_TENANT_DB_URL`` deliberately unbound, which is the exact condition
    that crash-looped the plane. It is the same harness the OMN-17557 diagnosis
    used to enumerate the eight, run forward instead of backward.
    """
    for key, value in _ONEX_DEV_POD_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("ONEX_TENANT_DB_URL", raising=False)

    topology = load_topology_profile("onex-dev")
    manifest = discover_contracts()

    owned_by_interim_profile = {
        contract.name
        for contract in filter_manifest_for_runtime_profile(
            manifest, INTERIM_PROFILE
        ).manifest.contracts
    }
    assert owned_by_interim_profile == set(INTERIM_REMOVED_CONTRACTS)

    for profile in ("main", "effects"):
        owned = filter_manifest_for_runtime_profile(manifest, profile).manifest
        owned_names = {contract.name for contract in owned.contracts}
        still_present = owned_names & set(INTERIM_REMOVED_CONTRACTS)
        assert not still_present, (
            f"{sorted(still_present)} are still owned by the {profile!r} "
            "profile, so the shared runtime will resolve the tenant_projection "
            "DSN and die again."
        )

        # Only db_io contracts can raise the missing-binding error, and wiring
        # the full profile in a unit test would need the whole container.
        subset = tuple(
            contract
            for contract in owned.contracts
            if contract.db_io is not None and contract.db_io.db_tables
        )
        # Under strict mode a failure does not come back in the report -- it
        # raises, exactly as it does in the pod's boot path. Both shapes are
        # collected so the assertions below read the same set either way.
        strict_raise: str | None = None
        report = None
        try:
            report = await wire_from_manifest(
                manifest=ModelAutoWiringManifest(contracts=subset),
                dispatch_engine=MessageDispatchEngine(),
                event_bus=_StubEventBus(),
                container=ModelONEXContainer(),
                subscribe_immediately=False,
                topology=topology,
            )
        except ModelOnexError as exc:
            strict_raise = str(exc)

        if strict_raise is None:
            assert report is not None
            failed = {
                result.contract_name
                for result in report.results
                if str(result.outcome).endswith("FAILED")
            }
        else:
            failed = _contract_names_in_strict_raise(strict_raise, owned_names)
            assert failed, (
                f"profile {profile!r} raised under strict mode but no contract "
                f"name could be parsed out of it: {strict_raise}"
            )

        assert not (failed & set(INTERIM_REMOVED_CONTRACTS)), (
            f"profile {profile!r}: {sorted(failed & set(INTERIM_REMOVED_CONTRACTS))} "
            "still fail auto-wiring despite being moved off this profile."
        )
        if _INFRA_HAS_ZERO_ROUTE_EXEMPTION:
            assert not failed, (
                f"profile {profile!r} must wire clean under strict mode; "
                f"failed={sorted(failed)}"
            )
        else:
            assert failed <= _OMN16976_UNRELEASED_WHEEL_RESIDUAL, (
                f"profile {profile!r} failed on contracts outside the known "
                "OMN-16976 unreleased-wheel residual "
                f"{sorted(_OMN16976_UNRELEASED_WHEEL_RESIDUAL)}: "
                f"{sorted(failed - _OMN16976_UNRELEASED_WHEEL_RESIDUAL)}"
            )


@pytest.mark.unit
def test_interim_docstring_names_the_reverting_ticket() -> None:
    """This file must delete itself with OMN-17556, and say so."""
    text = Path(__file__).read_text(encoding="utf-8")
    assert re.search(rf"deleted by the {REVERTING_TICKET} PR", text), (
        "the module docstring must state that the "
        f"{REVERTING_TICKET} PR deletes this file"
    )
