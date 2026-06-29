from pathlib import Path

import yaml

CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/api_contract.yaml"
)

NODE_CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)


def _exposure_columns(topic: str) -> list[str]:
    data = yaml.safe_load(NODE_CONTRACT_PATH.read_text())
    for exposure in data["projection_api"]["exposures"]:
        if exposure["topic"] == topic:
            return list(exposure["columns"])
    raise AssertionError(f"No projection_api exposure declared for topic {topic!r}")


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


# OMN-12748: the dashboard renders the delegated prompt/response by reading the
# contract-declared per-correlation detail projection
# (/projection/onex.snapshot.projection.delegation.correlation-trace.v1?correlation_id=...),
# not a bespoke REST route. That detail exposure must carry the delegation_events
# content columns, while the high-frequency list poll (decisions.v1) stays lean.

CORRELATION_TRACE_TOPIC = "onex.snapshot.projection.delegation.correlation-trace.v1"


def test_correlation_trace_exposure_includes_prompt_and_response():
    columns = _exposure_columns(CORRELATION_TRACE_TOPIC)
    assert "prompt_text" in columns, "correlation-trace.v1 must expose prompt_text"
    assert "response_text" in columns, "correlation-trace.v1 must expose response_text"
    assert "context_pack_hash" in columns, (
        "correlation-trace.v1 must expose context_pack_hash for context ON/OFF ROI"
    )
    assert "correlation_id" in columns, (
        "correlation-trace.v1 must expose correlation_id for ?correlation_id= filtering"
    )


def test_decisions_summary_exposure_excludes_content():
    columns = _exposure_columns("onex.snapshot.projection.delegation.decisions.v1")
    assert "prompt_text" not in columns, (
        "decisions.v1 is the high-frequency list poll; it must not carry prompt_text"
    )
    assert "response_text" not in columns, (
        "decisions.v1 is the high-frequency list poll; it must not carry response_text"
    )
