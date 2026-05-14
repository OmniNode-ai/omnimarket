from pathlib import Path

import yaml

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/api_contract.yaml"
)

REQUIRED_ENDPOINTS = [
    "delegation-summary",
    "recent-delegations",
    "model-routing",
    "quality-gate",
    "savings",
]


def test_api_contract_exists():
    assert CONTRACT_PATH.exists(), f"api_contract.yaml not found at {CONTRACT_PATH}"


def test_api_contract_declares_required_endpoints():
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    endpoints = data["endpoints"]
    for ep in REQUIRED_ENDPOINTS:
        assert ep in endpoints, f"Missing endpoint: {ep}"


def test_api_contract_declares_schema_version():
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert "schema_version" in data


def test_api_contract_declares_freshness_sla():
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    assert "freshness_sla_ms" in data
    assert isinstance(data["freshness_sla_ms"], int)
    assert data["freshness_sla_ms"] > 0


def test_api_contract_endpoint_paths_are_valid():
    data = yaml.safe_load(CONTRACT_PATH.read_text())
    for name, ep in data["endpoints"].items():
        assert "path" in ep, f"Endpoint {name} missing 'path'"
        assert ep["path"].startswith("/"), f"Endpoint {name} path must start with /"
        assert "method" in ep, f"Endpoint {name} missing 'method'"
        assert ep["method"] == "GET", f"Endpoint {name} must use GET"
        assert "response_schema" in ep, f"Endpoint {name} missing 'response_schema'"
