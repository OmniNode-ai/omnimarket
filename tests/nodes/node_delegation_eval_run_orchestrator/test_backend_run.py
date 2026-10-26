# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A pinned lab generation must attest its quant and never reuse answer labels."""

import pytest

from omnimarket.nodes.node_delegation_eval_run_orchestrator.backend_run import (
    BackendPin,
    attest_backend,
    generate_item,
)


def pin(quant: str = "Q8_0") -> BackendPin:
    return BackendPin(
        backend_id="mac-q8",
        endpoint="http://model.invalid:8130",
        host="mac",
        model="Qwen3.8-27B",
        model_file="/models/qwen.gguf",
        quant=quant,
    )


def test_attestation_rejects_same_alias_with_wrong_quant() -> None:
    def read(path, body=None):
        return (
            {"model_path": "/models/qwen.gguf", "model_alias": "Qwen3.8-27B"}
            if path == "/props"
            else {"data": [{"id": "Qwen3.8-27B", "meta": {"ftype": "Q3_K_M"}}]}
        )

    with pytest.raises(ValueError, match="quant"):
        attest_backend(pin(), read)


def test_attestation_rejects_wrong_model_file() -> None:
    def read(path, body=None):
        return {"model_path": "/another.gguf", "model_alias": "Qwen3.8-27B"}

    with pytest.raises(ValueError, match="model_file"):
        attest_backend(pin(), read)


def test_generation_retains_pin_latency_and_discards_historical_label() -> None:
    calls = []

    def read(path, body=None):
        calls.append((path, body))
        return {
            "model": "Qwen3.8-27B",
            "choices": [{"message": {"content": "fresh"}, "finish_reason": "stop"}],
        }

    row = generate_item(
        pin(),
        {
            "item_key": "i",
            "task_class": "document",
            "stratum": "all",
            "prompt_snapshot": "p",
            "label": "adequate",
        },
        read,
        max_tokens=128,
    )
    assert row["label"] == "unlabelable"
    assert row["response_snapshot"] == "fresh"
    assert row["backend_configuration"]["quant"] == "Q8_0"
    assert row["latency_ms"] >= 0
    assert len(calls) == 1
    assert calls[0][1]["model"] == "Qwen3.8-27B"


def test_transport_failure_is_incomplete_without_retry_or_error_body() -> None:
    calls = []

    def read(path, body=None):
        calls.append(path)
        raise OSError("secret-error-body")

    row = generate_item(
        pin(),
        {
            "item_key": "i",
            "task_class": "test",
            "stratum": "all",
            "prompt_snapshot": "p",
        },
        read,
        max_tokens=128,
    )
    assert row["error"] == "OSError"
    assert row["response_snapshot"] is None
    assert "secret-error-body" not in str(row)
    assert len(calls) == 1


def test_pin_rejects_endpoint_credentials() -> None:
    with pytest.raises(ValueError, match="endpoint"):
        BackendPin(
            backend_id="x",
            endpoint="http://key@host:8130",
            host="h",
            model="m",
            model_file="f",
            quant="q",
        )
