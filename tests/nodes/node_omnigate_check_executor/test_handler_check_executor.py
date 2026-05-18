# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerCheckExecutor."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from omnimarket.nodes.node_omnigate_check_executor.handlers.handler_check_executor import (
    HandlerCheckExecutor,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("00000000-0000-4000-a000-000000000145")


@pytest.mark.asyncio
async def test_check_executor_uses_injected_dependencies(tmp_path: Path) -> None:
    calls: list[tuple[object, Path]] = []
    config = SimpleNamespace(receipt=SimpleNamespace(advisory_blocks=False))

    def load_config(config_path: Path) -> object:
        assert config_path == tmp_path / ".omnigate.yaml"
        return config

    def execute_checks(loaded_config: object, repo_path: Path) -> tuple[object, ...]:
        calls.append((loaded_config, repo_path))
        return (
            SimpleNamespace(
                name="lint",
                command="ruff check",
                status="PASS",
                duration_ms=12,
                stdout_preview="ok",
                stdout_hash=None,
            ),
        )

    result = await HandlerCheckExecutor(
        config_loader=load_config,
        check_executor=execute_checks,
    ).handle(
        _CORRELATION_ID,
        str(tmp_path / ".omnigate.yaml"),
        str(tmp_path),
    )

    assert result.all_passed is True
    assert result.checks[0].name == "lint"
    assert calls == [(config, tmp_path)]


@pytest.mark.asyncio
async def test_advisory_blocks_when_config_policy_says_so(tmp_path: Path) -> None:
    config = SimpleNamespace(receipt=SimpleNamespace(advisory_blocks=True))

    def load_config(config_path: Path) -> object:
        assert config_path == tmp_path / ".omnigate.yaml"
        return config

    def execute_checks(loaded_config: object, repo_path: Path) -> tuple[object, ...]:
        assert loaded_config is config
        assert repo_path == tmp_path
        return (SimpleNamespace(name="scan", command="scan", status="ADVISORY"),)

    result = await HandlerCheckExecutor(
        config_loader=load_config,
        check_executor=execute_checks,
    ).handle(
        _CORRELATION_ID,
        str(tmp_path / ".omnigate.yaml"),
        str(tmp_path),
    )

    assert result.all_passed is False
    assert result.checks[0].status == "ADVISORY"
