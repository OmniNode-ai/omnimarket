from __future__ import annotations

from pathlib import Path

from scripts.validation.validate_contract_overlay_boundary import (
    validate_contract_overlay_boundary,
)


def _write_contract(root: Path, relative: str, content: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_fails_model_defaults_in_node_contract(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
model_routing:
  primary: qwen3-coder-30b
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_MODEL_LITERAL"
    assert findings[0].yaml_path == "model_routing.primary"


def test_fails_served_model_id_key_even_with_unknown_value(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  served_model_id: runtime-owned
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_MODEL_OR_ENDPOINT_KEY"
    assert findings[0].yaml_path == "config.served_model_id"


def test_fails_llm_endpoint_literal(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
config:
  endpoint: http://llm-endpoint.example.invalid:8000
""",
    )

    findings = validate_contract_overlay_boundary(tmp_path)

    assert len(findings) == 1
    assert findings[0].reason == "FORBIDDEN_ENDPOINT_LITERAL"


def test_allows_policy_schema_without_defaults(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
model_routing_policy_schema:
  source: injected_ModelRoutingPolicy
  no_contract_model_defaults: true
  required_fields:
    - primary
    - timeout_per_attempt_s
""",
    )

    assert validate_contract_overlay_boundary(tmp_path) == []


def test_ignores_description_text(tmp_path: Path) -> None:
    _write_contract(
        tmp_path,
        "src/omnimarket/nodes/node_example/contract.yaml",
        """
name: node_example
description: "Historical note mentions claude-sonnet but is not config."
""",
    )

    assert validate_contract_overlay_boundary(tmp_path) == []
