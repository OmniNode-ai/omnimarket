from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

NODES_ROOT = Path(__file__).resolve().parents[2] / "src/omnimarket/nodes"
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
        if contract_path.parent.name in PENDING_RUNTIME_OWNERSHIP_NODES:
            continue
        contract = _load_contract(contract_path)
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
