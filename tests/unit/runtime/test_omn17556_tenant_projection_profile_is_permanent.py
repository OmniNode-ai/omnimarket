# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17556 -- the tenant-projection profile stands on its own registration.

What this guards
----------------
Eight omnimarket projection contracts write TENANT-domain tables, so their
handlers resolve the ``tenant_projection`` binding. That binding's credential is
a store-resolved ``secret_ref`` read through ``SecretResolver`` at the binding
boundary, and exactly one process holds it: the consolidated writer that boots
``RUNTIME_PROFILE=tenant-projection``. The eight therefore name that profile and
only that profile.

Under OMN-17641 the same eight named the same profile for a DIFFERENT reason --
``tenant-projection`` was NOT registered, so ``runtime_profile_unregistered``
fired on all eight and an interim block in
``validation/runtime_profiles_allowlist.yaml`` recorded the orphaning as
intended. omnibase-core 0.47.3 registers the profile, which retires that block:
an exemption for a rule that no longer fires is a stale exemption, and a stale
exemption is indistinguishable from a live one to the next reader.

The load-bearing assertion here is the NEGATIVE one:
``test_no_contract_is_carried_by_an_allowlist_exemption``. Deleting the interim
block is only safe if the eight survive WITHOUT it, which is true only while
``tenant-projection`` is in ``REGISTERED_RUNTIME_PROFILES``. If a future core
floor ever drops back below 0.47.3, that test is what fails -- loudly, naming
the profile -- instead of the eight silently reverting to unregistered orphans.

OMN-20558 then deleted the drained allowlist file itself, so the negative
assertion now reads: the file is absent, and each of the eight passes the
validator with an explicitly empty allowlist.

Every zero this module asserts (zero allowlist entries, zero interim
vocabulary) is paired with a positive control in the same test, because an
empty result from a mis-shaped lookup reads exactly like a clean bill of
health.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
import yaml
from omnibase_core.constants.constants_runtime_profiles import (
    CONSUMER_ATTACHED_RUNTIME_PROFILES,
    REGISTERED_RUNTIME_PROFILES,
)
from omnibase_core.validation.validator_runtime_profiles import (
    ValidatorRuntimeProfiles,
    load_default_allowlist,
)

PROFILE: Final[str] = "tenant-projection"
OWNING_TICKET: Final[str] = "OMN-17556"
RETIRED_TICKET: Final[str] = "OMN-17641"

# node directory -> the `name:` its contract.yaml declares.
TENANT_PROJECTION_CONTRACTS: Final[dict[str, str]] = {
    "node_canary_score_reducer": "canary_score_reducer",
    "node_hook_event_capture": "node_hook_event_capture",
    "node_projection_context_roi": "projection_context_roi",
    "node_projection_cost_summary": "node_projection_cost_summary",
    "node_projection_delegation_inference_response": (
        "node_projection_delegation_inference_response"
    ),
    "node_projection_dep_health": "node_projection_dep_health",
    "node_projection_pattern_learning": "projection_pattern_learning",
    "node_projection_routing_decision": "projection_routing_decision",
}

# tests/unit/runtime/<this file> -> parents[3] == repo root.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_NODES_DIR: Final[Path] = _REPO_ROOT / "src" / "omnimarket" / "nodes"
_ALLOWLIST_PATH: Final[Path] = (
    _REPO_ROOT / "validation" / "runtime_profiles_allowlist.yaml"
)


def _contract_path(node_dir: str) -> Path:
    path = _NODES_DIR / node_dir / "contract.yaml"
    if not path.is_file():
        raise AssertionError(
            f"contract for {node_dir!r} not found at {path} -- the "
            f"{OWNING_TICKET} node-dir mapping is stale."
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


def _allowlist_node_ids(path: Path = _ALLOWLIST_PATH) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path} did not parse as a mapping"
    entries = raw.get("allowlist") or raw.get("entries") or []
    if not isinstance(entries, list):
        raise AssertionError(
            f"{path}: expected a list of entries, got "
            f"{type(entries).__name__}. The lookup shape changed; a zero from "
            "this function would be a false clean bill of health."
        )
    return [
        str(e["node_id"]) for e in entries if isinstance(e, dict) and "node_id" in e
    ]


def _carried(node_ids: list[str]) -> list[str]:
    return sorted(
        set(node_ids)
        & (set(TENANT_PROJECTION_CONTRACTS) | set(TENANT_PROJECTION_CONTRACTS.values()))
    )


@pytest.mark.unit
def test_profile_is_registered_in_the_resolved_core() -> None:
    """The registration is what makes the allowlist exemption unnecessary.

    This is the precondition every other assertion in this module rests on. It
    is asserted against the RESOLVED core, not against a repo file, because the
    validator that would otherwise fire reads the same resolved constant.
    """
    assert PROFILE in REGISTERED_RUNTIME_PROFILES, (
        f"{PROFILE!r} is not in REGISTERED_RUNTIME_PROFILES of the resolved "
        f"omnibase-core. It is registered from 0.47.3 onward; a floor below "
        f"that re-orphans all {len(TENANT_PROJECTION_CONTRACTS)} contracts "
        f"below. Registered profiles seen: {sorted(REGISTERED_RUNTIME_PROFILES)}"
    )
    assert PROFILE in CONSUMER_ATTACHED_RUNTIME_PROFILES, (
        f"{PROFILE!r} must be consumer-attached: a real writer process boots "
        "it and drains these contracts. Absence here would mean the profile "
        "is registered but nothing consumes it, which is the orphaning this "
        "change exists to end."
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_dir", sorted(TENANT_PROJECTION_CONTRACTS))
def test_each_contract_declares_exactly_the_tenant_projection_profile(
    node_dir: str,
) -> None:
    path = _contract_path(node_dir)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), f"{path} did not parse as a mapping"
    assert raw["name"] == TENANT_PROJECTION_CONTRACTS[node_dir]

    assert _declared_profiles(raw) == (PROFILE,), (
        f"{node_dir} must declare exactly [{PROFILE}] so "
        "filter_manifest_for_runtime_profile routes it to the consolidated "
        "writer and keeps it out of `main` and `effects`, neither of which "
        f"carries the credential; got {_declared_profiles(raw)!r}"
    )


@pytest.mark.unit
@pytest.mark.parametrize("node_dir", sorted(TENANT_PROJECTION_CONTRACTS))
def test_each_contract_states_permanent_ownership_not_interim_orphaning(
    node_dir: str,
) -> None:
    """The comment must read as a boundary, not as scaffolding.

    A bare ``runtime_profiles: [tenant-projection]`` is indistinguishable from
    the OMN-12950 accident -- a contract naming a profile nothing boots and
    silently writing nothing forever. The citation is what makes the routing
    legible. The retired ticket must be gone from the same block: a comment
    promising a revert that already happened sends the next reader looking for
    work that does not exist.
    """
    text = _contract_path(node_dir).read_text(encoding="utf-8")
    assert OWNING_TICKET in text, (
        f"{node_dir} declares {PROFILE!r} without citing {OWNING_TICKET}, the "
        "ticket that made the routing permanent."
    )
    # Positive control for the negative assertion below: the file is non-empty
    # and this scan really does find ticket references when they are present.
    assert text.count(OWNING_TICKET) >= 1
    assert RETIRED_TICKET not in text, (
        f"{node_dir} still cites {RETIRED_TICKET}, whose interim block and "
        "paired test are deleted by this change. The citation is stale."
    )


@pytest.mark.unit
def test_no_contract_is_carried_by_an_allowlist_exemption(
    tmp_path: Path,
) -> None:
    """The eight survive on the registration alone -- the load-bearing check.

    Deleting the interim block is only safe while this holds. If it ever stops
    holding, the failure names the profile instead of leaving eight contracts
    silently registered-but-undrained.
    """
    # POSITIVE CONTROL, per the empty-result-is-not-evidence rule: a planted
    # tenant-projection row must be visible to the lookup, and the lookup must
    # agree with the validator's own loader. Without these, a renamed key would
    # make the zero asserted below pass vacuously against an empty list.
    synthetic = tmp_path / "runtime_profiles_allowlist.yaml"
    synthetic.write_text(
        "allowlist:\n  - node_id: canary_score_reducer\n    reason: positive control\n",
        encoding="utf-8",
    )
    planted = _allowlist_node_ids(synthetic)
    assert planted == ["canary_score_reducer"], (
        "the lookup must see a planted tenant-projection row, else the zero "
        "below is not evidence."
    )
    assert _carried(planted) == ["canary_score_reducer"], (
        "the lookup must see a planted tenant-projection row, else the zero "
        "below is not evidence."
    )
    assert set(planted) == set(load_default_allowlist(synthetic)), (
        "this lookup must agree with the validator's own loader."
    )

    # OMN-20558 deleted the repo allowlist outright, so no contract can be
    # carried by it. Positive control: the directory it lived in still exists,
    # so the absence is of the file and not of a mis-built path.
    assert _ALLOWLIST_PATH.parent.is_dir(), f"{_ALLOWLIST_PATH.parent} is missing"
    assert not _ALLOWLIST_PATH.exists(), (
        f"{_ALLOWLIST_PATH} is back. The runtime_profiles allowlist drained to "
        "empty and was deleted (OMN-20558); an exception list must not return."
    )

    # The load-bearing half: each of the eight passes the validator with an
    # explicitly empty allowlist, so it stands on the registration alone.
    for node_dir in sorted(TENANT_PROJECTION_CONTRACTS):
        result = ValidatorRuntimeProfiles(allowlist=set()).validate(
            _contract_path(node_dir).parent
        )
        assert result.is_valid, (
            f"{node_dir} fails runtime_profiles with no allowlist: "
            f"{[i.message for i in result.issues]}. {PROFILE!r} is registered, "
            "so nothing should need an exemption."
        )


@pytest.mark.unit
def test_the_retired_interim_scaffolding_is_gone() -> None:
    """Neither the interim block nor its test survives this change."""
    # The whole allowlist, interim block included, is gone (OMN-20558).
    # Positive control: the directory that held it is still there.
    assert _ALLOWLIST_PATH.parent.is_dir(), f"{_ALLOWLIST_PATH.parent} is missing"
    assert not _ALLOWLIST_PATH.exists(), (
        f"{_ALLOWLIST_PATH} exists again; the {RETIRED_TICKET} interim block "
        "lived there and the file was deleted."
    )

    retired_test = (
        _REPO_ROOT
        / "tests"
        / "unit"
        / "runtime"
        / "test_omn17641_interim_tenant_projection_profile.py"
    )
    assert not retired_test.exists(), (
        f"{retired_test} asserts the interim state this change ends; it must "
        "be deleted, not left to fail."
    )
    # Positive control: this module itself exists on the same path root, so a
    # bad _REPO_ROOT cannot make the assertion above pass vacuously.
    assert Path(__file__).resolve().parent == retired_test.parent
