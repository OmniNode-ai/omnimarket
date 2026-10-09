# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard main-only ownership of lane-scoped contracts (OMN-17427).

No compose lane's effects runtime declares ONEX_RUNTIME_LANE: omnibase_infra
OMN-19144 keeps that declaration on omninode-runtime only. Under OMN-19408,
discovering a lane-scoped contract owned by effects fails closed and holds
runtime-effects Docker-unhealthy on every lane. The main runtime declares the
lane and carries OMNINODE_INTERNAL_DB_URL and the ONEXBOT_OCC App credential
read by the branch claim check node. Its contract must therefore belong to main,
following the same reasoning as node_pr_landing_orchestrator's contract.
"""

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit


def _discover_lane_scoped_contracts() -> dict[Path, tuple[str, ...]]:
    nodes_dir = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
    contracts: dict[Path, tuple[str, ...]] = {}
    for contract_path in sorted(nodes_dir.glob("*/contract.yaml")):
        contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if "runtime_lanes" not in contract:
            continue
        profiles = contract.get("runtime_profiles")
        if profiles is None:
            profiles = contract.get("descriptor", {}).get("runtime_profiles")
        if profiles is None:
            profiles = ("main",)
        elif isinstance(profiles, str):
            profiles = [profiles]
        contracts[contract_path.parent] = tuple(
            profile.strip().lower() for profile in profiles
        )
    return contracts


LANE_SCOPED_CONTRACTS = _discover_lane_scoped_contracts()


def test_lane_scoped_contracts_exist() -> None:
    node_names = {contract_dir.name for contract_dir in LANE_SCOPED_CONTRACTS}
    assert node_names
    assert "node_branch_claim_check_effect" in node_names


@pytest.mark.parametrize(
    "contract_dir",
    LANE_SCOPED_CONTRACTS,
    ids=[contract_dir.name for contract_dir in LANE_SCOPED_CONTRACTS],
)
def test_every_lane_scoped_contract_is_owned_by_main_only(contract_dir: Path) -> None:
    profiles = LANE_SCOPED_CONTRACTS[contract_dir]
    assert profiles == ("main",), (
        f"{contract_dir.name} has runtime profiles {profiles!r}; lane-scoped contracts "
        "must belong to main only (OMN-17427). OMN-19144 keeps ONEX_RUNTIME_LANE "
        "on omninode-runtime only, so effects ownership causes fail-closed "
        "discovery under OMN-19408 and holds runtime-effects Docker-unhealthy."
    )
