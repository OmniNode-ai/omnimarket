from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NODES_ROOT = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"
# Remove an entry once the lane's effects runtime declares ONEX_RUNTIME_LANE.
LANE_SCOPED_MAIN_OWNED_NODES: dict[str, str] = {
    "node_pr_landing_github_effect": (
        "OMN-19408 lane-scopes it to compose-dev; under OMN-19144 the effects "
        "runtime declares no lane, so the main runtime owns it."
    ),
    "node_pr_handoff_ledger_effect": (
        "The lab effects runtimes declare no ONEX_RUNTIME_LANE, so the main "
        "runtime owns it, as node_pr_landing_github_effect."
    ),
    "node_branch_claim_check_effect": (
        "OMN-17427: the lab effects runtimes declare no ONEX_RUNTIME_LANE, so "
        "the main runtime owns it, as node_pr_landing_github_effect."
    ),
}
PENDING_RUNTIME_OWNERSHIP_NODES = {
    "node_build_loop_orchestrator",
}


def _load_contract(contract_path: Path) -> dict[str, Any]:
    with contract_path.open() as handle:
        contract = yaml.safe_load(handle)
    assert isinstance(contract, dict), f"{contract_path} must load as a YAML mapping"
    return contract


def _subscribes_to_command_topics(contract: dict[str, Any]) -> bool:
    event_bus = contract.get("event_bus")
    if not isinstance(event_bus, dict):
        return False
    subscribe_topics = event_bus.get("subscribe_topics")
    if not isinstance(subscribe_topics, list):
        return False
    return any(
        isinstance(topic, str) and ".cmd." in topic for topic in subscribe_topics
    )


def _requires_runtime_profiles(contract: dict[str, Any]) -> bool:
    if not _subscribes_to_command_topics(contract):
        return False

    descriptor = contract.get("descriptor")
    descriptor = descriptor if isinstance(descriptor, dict) else {}

    node_type = str(contract.get("node_type") or "").lower()
    archetype = str(descriptor.get("node_archetype") or "").lower()
    purity = str(descriptor.get("purity") or "").lower()

    if "effect" in node_type or archetype == "effect":
        return True
    if "workflow" in node_type or archetype == "workflow":
        return True
    if "service" in node_type or archetype == "service":
        return True
    if purity in {"effectful", "impure", "side_effect"} and (
        "orchestrator" in node_type or archetype == "orchestrator"
    ):
        return True
    return False


def test_effectful_command_consumers_declare_effects_runtime_profile() -> None:
    missing_profiles: list[str] = []

    for contract_path in sorted(NODES_ROOT.glob("node_*/contract.yaml")):
        node_name = contract_path.parent.name
        contract = _load_contract(contract_path)
        if node_name in LANE_SCOPED_MAIN_OWNED_NODES:
            assert _requires_runtime_profiles(contract), (
                f"{node_name} no longer requires runtime profiles; remove it from "
                "LANE_SCOPED_MAIN_OWNED_NODES"
            )
            assert contract.get("runtime_profiles") == ["main"], (
                f"{node_name} must have top-level runtime_profiles exactly ['main']; "
                "remove it from LANE_SCOPED_MAIN_OWNED_NODES if ownership changed"
            )
            runtime_lanes = contract.get("runtime_lanes")
            assert isinstance(runtime_lanes, list), (
                f"{node_name} must have a top-level runtime_lanes list; remove it "
                "from LANE_SCOPED_MAIN_OWNED_NODES if lane scoping changed"
            )
            assert runtime_lanes, (
                f"{node_name} must have a non-empty top-level runtime_lanes list; "
                "remove it from LANE_SCOPED_MAIN_OWNED_NODES if lane scoping changed"
            )
            continue
        if node_name in PENDING_RUNTIME_OWNERSHIP_NODES:
            continue
        if not _requires_runtime_profiles(contract):
            continue

        descriptor = contract.get("descriptor")
        assert isinstance(descriptor, dict), (
            f"{contract_path.parent.name} must declare descriptor for runtime ownership"
        )

        runtime_profiles = descriptor.get("runtime_profiles")
        if not isinstance(runtime_profiles, list) or "effects" not in runtime_profiles:
            missing_profiles.append(contract_path.parent.name)

    assert missing_profiles == [], (
        "Command-consuming effectful nodes must declare "
        f"descriptor.runtime_profiles including 'effects': {missing_profiles}"
    )


def test_lane_scoped_main_owned_nodes_exist() -> None:
    for node_name in LANE_SCOPED_MAIN_OWNED_NODES:
        contract_path = NODES_ROOT / node_name / "contract.yaml"
        assert contract_path.is_file(), (
            f"{node_name} has no contract.yaml; remove it from "
            "LANE_SCOPED_MAIN_OWNED_NODES"
        )
