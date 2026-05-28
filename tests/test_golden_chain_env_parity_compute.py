"""Golden-chain coverage for node_env_parity_compute native runtime dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.adapters.codex.runtime_client import CodexRuntimeRequestAdapter

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = (
    _REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_env_parity_compute"
    / "contract.yaml"
)


def _complete_lane(runtime_suffix: str) -> dict[str, str]:
    return {
        "ENABLE_KAFKA": "true",
        "KAFKA_BOOTSTRAP_SERVERS": f"redpanda-{runtime_suffix}:9092",
        "KAFKA_CONSUMER_GROUP": f"omnimarket-{runtime_suffix}",
        "ENABLE_POSTGRES": "true",
        "POSTGRES_HOST": f"postgres-{runtime_suffix}",
        "POSTGRES_PORT": "5432",
        "POSTGRES_DATABASE": "omnimarket",
        "POSTGRES_USER": "omnimarket",
        "POSTGRES_PASSWORD": f"secret-{runtime_suffix}",
        "ENABLE_QDRANT": "false",
        "QDRANT_HOST": f"qdrant-{runtime_suffix}",
        "QDRANT_PORT": "6333",
        "ENABLE_VALKEY": "false",
        "VALKEY_HOST": f"valkey-{runtime_suffix}",
        "VALKEY_PORT": "6379",
        "ENABLE_MEMORY_SERVICE": "false",
        "EMBEDDING_MODEL_URL": f"http://embedding-{runtime_suffix}:8000",
        "ONEX_TARGET_RUNTIME_ADDRESS": f"runtime://{runtime_suffix}",
    }


def _dispatch_env_parity(payload: dict[str, object]) -> dict[str, object]:
    response = CodexRuntimeRequestAdapter().dispatch_sync(
        command_name="env_parity_compute",
        payload=payload,
        runtime_selection="local",
        timeout_ms=30_000,
    )

    assert response.ok is True
    assert response.runtime_mode == "local"
    assert response.runtime_evidence is not None
    assert response.runtime_evidence.node_contract == str(_CONTRACT_PATH)
    assert response.runtime_evidence.command_topic == (
        "onex.cmd.omnimarket.env-parity-compute-start.v1"
    )
    assert response.runtime_evidence.terminal_topic == (
        "onex.evt.omnimarket.env-parity-compute-completed.v1"
    )
    assert response.output_payloads is not None
    return response.output_payloads[0]


@pytest.mark.unit
def test_contract_declares_native_runtime_surface() -> None:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert raw.get("node_not_implemented") is None
    assert raw["handler"]["class"] == "HandlerEnvParityCompute"
    assert raw["terminal_event"] == (
        "onex.evt.omnimarket.env-parity-compute-completed.v1"
    )
    assert raw["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.env-parity-compute-start.v1"
    ]
    assert raw["env_parity"]["lanes"] == ["dev", "staging", "prod"]
    assert {item["name"] for item in raw["env_parity"]["variables"]} >= {
        "ENABLE_KAFKA",
        "KAFKA_BOOTSTRAP_SERVERS",
        "POSTGRES_HOST",
        "ONEX_TARGET_RUNTIME_ADDRESS",
    }


@pytest.mark.unit
def test_runtime_dispatch_reports_passed_parity_for_complete_lane_snapshots() -> None:
    payload = {
        "scope": "omnimarket-runtime",
        "env_by_lane": {
            "dev": _complete_lane("dev"),
            "staging": _complete_lane("staging"),
            "prod": _complete_lane("prod"),
        },
    }

    result = _dispatch_env_parity(payload)

    assert result["status"] == "passed"
    assert result["parity_ok"] is True
    assert result["scope"] == "omnimarket-runtime"
    assert result["lanes_checked"] == ["dev", "staging", "prod"]
    assert result["gaps"] == []
    assert "KAFKA_BOOTSTRAP_SERVERS" in result["variables_checked"]
    assert result["correlation_id"]


@pytest.mark.unit
def test_runtime_dispatch_flags_missing_env_and_settings_errors() -> None:
    staging = _complete_lane("staging")
    staging["KAFKA_BOOTSTRAP_SERVERS"] = ""
    staging["POSTGRES_PASSWORD"] = ""

    payload = {
        "scope": "omnimarket-runtime",
        "variable_names": ["KAFKA_BOOTSTRAP_SERVERS", "POSTGRES_PASSWORD"],
        "env_by_lane": {
            "dev": _complete_lane("dev"),
            "staging": staging,
            "prod": _complete_lane("prod"),
        },
    }

    result = _dispatch_env_parity(payload)

    assert result["status"] == "gaps_detected"
    assert result["parity_ok"] is False
    assert {
        (gap["lane"], gap["variable_name"], gap["reason"]) for gap in result["gaps"]
    } >= {
        ("staging", "KAFKA_BOOTSTRAP_SERVERS", "missing_required_env"),
        ("staging", "POSTGRES_PASSWORD", "missing_required_env"),
        ("staging", "Settings", "settings_validation"),
    }


@pytest.mark.unit
def test_runtime_dispatch_flags_contract_consistency_mismatch() -> None:
    staging = _complete_lane("staging")
    staging["ENABLE_KAFKA"] = "false"

    payload = {
        "scope": "omnimarket-runtime",
        "variable_names": ["ENABLE_KAFKA"],
        "env_by_lane": {
            "dev": _complete_lane("dev"),
            "staging": staging,
            "prod": _complete_lane("prod"),
        },
    }

    result = _dispatch_env_parity(payload)

    assert result["status"] == "gaps_detected"
    assert result["parity_ok"] is False
    assert any(
        gap["variable_name"] == "ENABLE_KAFKA" and gap["reason"] == "value_mismatch"
        for gap in result["gaps"]
    )


@pytest.mark.unit
def test_disabled_services_do_not_require_service_specific_env() -> None:
    lane = {
        "ENABLE_KAFKA": "false",
        "ENABLE_POSTGRES": "false",
        "ENABLE_QDRANT": "false",
        "ENABLE_VALKEY": "false",
        "ENABLE_MEMORY_SERVICE": "false",
        "ONEX_TARGET_RUNTIME_ADDRESS": "runtime://local",
    }

    result = _dispatch_env_parity(
        {
            "scope": "omnimarket-runtime",
            "env_by_lane": {"dev": lane, "staging": lane, "prod": lane},
        }
    )

    assert result["status"] == "passed"
    assert result["parity_ok"] is True
    assert result["gaps"] == []
