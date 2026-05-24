from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_model_router"
    / "contract.yaml"
)


def test_node_model_router_contract_declares_policy_schema_not_model_defaults() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert "model_routing" not in contract
    schema = contract["model_routing_policy_schema"]
    assert schema["source"] == "injected_ModelRoutingPolicy"
    assert schema["no_contract_model_defaults"] is True
    assert "primary" in schema["required_fields"]
    assert "fallback" in schema["optional_fields"]


def test_node_model_router_contract_has_no_served_model_or_endpoint_defaults() -> None:
    raw = CONTRACT_PATH.read_text(encoding="utf-8").lower()

    forbidden_fragments = [
        "default_model:",
        "served_model_id:",
        "qwen",
        "deepseek",
        "claude-sonnet",
        "gpt-",
        "gemini-",
        "http://",
        "https://",
        "base_url:",
    ]
    assert not any(fragment in raw for fragment in forbidden_fragments)
