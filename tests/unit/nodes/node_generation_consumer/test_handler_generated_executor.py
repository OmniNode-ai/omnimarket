# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerGeneratedExecutor — dynamic handler loading from sandbox."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from omnimarket.nodes.node_generation_consumer.handlers.handler_generated_executor import (
    HandlerGeneratedExecutor,
)

_VALID_HANDLER = """\
def handle(input_data):
    return {"echo": input_data.get("value", "none")}
"""

_NON_DICT_HANDLER = """\
def handle(input_data):
    return "just a string"
"""

_RAISING_HANDLER = """\
def handle(input_data):
    raise ValueError("boom")
"""

_NO_HANDLE_HANDLER = """\
def process(input_data):
    return {}
"""

_SYNTAX_ERROR_HANDLER = """\
def handle(input_data
    return {}
"""


def _write_handler(sandbox: Path, node_name: str, source: str) -> None:
    node_dir = sandbox / node_name
    node_dir.mkdir(parents=True, exist_ok=True)
    (node_dir / "handler.py").write_text(source)


@pytest.mark.unit
def test_execute_returns_result_from_valid_handler() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_echo", _VALID_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_echo", {"value": "hello"})

    assert result == {"echo": "hello"}


@pytest.mark.unit
def test_execute_returns_error_when_handler_missing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.execute("node_nonexistent", {})

    assert "error" in result
    assert "Handler not found" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_no_handle_function() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_no_handle", _NO_HANDLE_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_no_handle", {})

    assert "error" in result
    assert "missing handle()" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_handle_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_raises", _RAISING_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_raises", {})

    assert "error" in result
    assert "boom" in result["error"]


@pytest.mark.unit
def test_execute_returns_error_when_syntax_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_bad_syntax", _SYNTAX_ERROR_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_bad_syntax", {})

    assert "error" in result
    assert "Failed to load generated handler" in result["error"]


@pytest.mark.unit
def test_execute_wraps_non_dict_result() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_str_result", _NON_DICT_HANDLER)

        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)
        result = executor.execute("node_str_result", {})

    assert result == {"result": "just a string"}


@pytest.mark.unit
def test_execute_picks_up_updated_handler_without_reinit() -> None:
    """Re-importing on each call means an updated handler.py is picked up immediately."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        _write_handler(sandbox, "node_hot", _VALID_HANDLER)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        first = executor.execute("node_hot", {"value": "v1"})
        assert first == {"echo": "v1"}

        # Overwrite handler in place — simulates hot-reload write
        updated = "def handle(input_data):\n    return {'updated': True}\n"
        _write_handler(sandbox, "node_hot", updated)

        second = executor.execute("node_hot", {})
        assert second == {"updated": True}


@pytest.mark.unit
def test_default_sandbox_path_is_relative() -> None:
    """Default sandbox must not be an absolute machine-specific path."""
    executor = HandlerGeneratedExecutor()
    assert not executor.sandbox_dir.is_absolute()


# ---------------------------------------------------------------------------
# deploy() tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_deploy_writes_handler_and_contract_to_sandbox() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        result = executor.deploy(
            {
                "node_name": "node_sentiment",
                "contract_yaml": "name: node_sentiment\n",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-deploy-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        assert result["status"] == "ok"
        assert result["node_name"] == "node_sentiment"
        assert (sandbox / "node_sentiment" / "handler.py").read_text() == _VALID_HANDLER
        assert (sandbox / "node_sentiment" / "contract.yaml").exists()


@pytest.mark.unit
def test_deploy_registers_node_for_execution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        executor.deploy(
            {
                "node_name": "node_echo2",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-deploy-2",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        assert "node_echo2" in executor._registry
        assert executor._registry["node_echo2"] == sandbox / "node_echo2" / "handler.py"


@pytest.mark.unit
def test_deploy_then_execute_runs_generated_handler() -> None:
    """Full deploy→execute round-trip without any pre-written files."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        executor.deploy(
            {
                "node_name": "node_round_trip",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-rt-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        )

        result = executor.execute("node_round_trip", {"value": "deployed"})

    assert result == {"echo": "deployed"}


@pytest.mark.unit
def test_deploy_accepts_json_bytes_payload() -> None:
    import json

    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        executor = HandlerGeneratedExecutor(sandbox_dir=sandbox)

        raw = json.dumps(
            {
                "node_name": "node_bytes",
                "contract_yaml": "",
                "handler_source": _VALID_HANDLER,
                "correlation_id": "corr-bytes-1",
                "generated_contract_hash": "sha256:abc",
                "generated_handler_hash": "sha256:def",
            }
        ).encode()

        result = executor.deploy(raw)

    assert result["status"] == "ok"
    assert result["node_name"] == "node_bytes"


@pytest.mark.unit
def test_deploy_returns_error_on_missing_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {"handler_source": _VALID_HANDLER, "contract_yaml": ""}
        )

    assert "error" in result
    assert "missing node_name" in result["error"]


@pytest.mark.unit
def test_deploy_returns_error_on_missing_handler_source() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy({"node_name": "node_no_src", "contract_yaml": ""})

    assert "error" in result
    assert "missing handler_source" in result["error"]


@pytest.mark.unit
def test_deploy_returns_error_on_invalid_json_bytes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(b"not valid json {")

    assert "error" in result
    assert "Invalid deploy payload JSON" in result["error"]


@pytest.mark.unit
def test_deploy_rejects_path_traversal_in_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {
                "node_name": "../../etc/passwd",
                "handler_source": _VALID_HANDLER,
                "contract_yaml": "",
            }
        )

    assert "error" in result
    assert "unsafe" in result["error"]


@pytest.mark.unit
def test_deploy_rejects_absolute_node_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        executor = HandlerGeneratedExecutor(sandbox_dir=Path(tmp))
        result = executor.deploy(
            {
                "node_name": "/etc/pwned",
                "handler_source": _VALID_HANDLER,
                "contract_yaml": "",
            }
        )

    assert "error" in result
    assert "unsafe" in result["error"]
