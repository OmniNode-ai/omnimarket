# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: keep in-process tenant projection writers off runtime-main.

A lab runtime-main fails at boot when it needs the tenant_projection
binding, whose credential belongs to the tenant-projection writer process and
the dedicated projection-writer-* processes. A contract with no declared
profile is owned by main, so it counts as an offender too.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.topology import load_topology_profile

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


@pytest.fixture(scope="module")
def manifest() -> ModelAutoWiringManifest:
    with pytest.MonkeyPatch.context() as environment:
        environment.delenv("ONEX_ACTIVE_RUNTIME_PACKAGES", raising=False)
        discovered = discover_contracts_from_paths(
            sorted(_NODES.glob("*/contract.yaml"))
        )
    assert not discovered.errors, discovered.errors
    return discovered


@pytest.fixture(scope="module")
def tenant_projection_writers(
    manifest: ModelAutoWiringManifest,
) -> tuple[dict[str, tuple[str, ...]], set[str]]:
    topology = load_topology_profile("local")
    in_process: dict[str, tuple[str, ...]] = {}
    exempt: set[str] = set()
    for contract in manifest.contracts:
        if not contract.db_io or not contract.db_io.db_tables:
            continue
        try:
            target = handler_wiring._resolve_projection_database_target(
                contract.db_io.db_tables, topology
            )
        except ValueError:
            continue
        if not any(b.binding_ref == "tenant_projection" for b in target.bindings):
            continue
        if not contract.handler_routing:
            continue
        for entry in contract.handler_routing.handlers:
            cls = getattr(
                importlib.import_module(entry.handler.module), entry.handler.name
            )
            typed_def_b = handler_wiring._resolve_def_b_input_model_type(cls.handle)
            standalone = (
                callable(getattr(cls, "project_event", None))
                and callable(getattr(cls, "run", None))
                and not getattr(
                    cls, handler_wiring.PROJECTION_INPROCESS_DISPATCH_ATTR, False
                )
            )
            if typed_def_b is not None or standalone:
                exempt.add(contract.name)
            else:
                in_process[contract.name] = contract.runtime_profiles
    return in_process, exempt


def _holds_the_credential(profile: str) -> bool:
    return profile == "tenant-projection" or profile.startswith("projection-writer-")


def test_in_process_writers_stay_off_the_shared_runtimes(
    tenant_projection_writers: tuple[dict[str, tuple[str, ...]], set[str]],
) -> None:
    in_process, _ = tenant_projection_writers
    offenders = {
        name: profiles
        for name, profiles in sorted(in_process.items())
        if not profiles or not all(_holds_the_credential(p) for p in profiles)
    }
    assert not offenders, (
        f"tenant_projection in-process writers with wrong profiles: {offenders}"
    )


def test_derived_writers_and_exemptions_have_positive_controls(
    tenant_projection_writers: tuple[dict[str, tuple[str, ...]], set[str]],
) -> None:
    in_process, exempt = tenant_projection_writers
    assert in_process
    assert {
        "projection_run_trace",
        "projection_delegation_disposition",
        "projection_delegation_eval",
        "projection_metering_summary",
        "projection_provider_quota",
        "projection_routing_feedback",
    } <= in_process.keys()
    assert "node_delegation_routing_reducer" in exempt
