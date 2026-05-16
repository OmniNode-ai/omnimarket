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
