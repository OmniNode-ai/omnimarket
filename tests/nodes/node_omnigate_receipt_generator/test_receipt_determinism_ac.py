# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Determinism AC with the clock and checkout hashes held fixed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_omnigate_receipt_generator.handlers import (
    handler_receipt_generator,
)
from omnimarket.nodes.node_omnigate_receipt_generator.models.model_receipt_generator_input import (
    ModelReceiptGeneratorInput,
)

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_ac_generator_same_input_and_clock_produce_identical_receipt(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        project_name="Omni",
        project_url="https://github.com/org/repo",
        receipt=SimpleNamespace(signing="none"),
    )
    handler = handler_receipt_generator.HandlerReceiptGenerator(
        config_loader=lambda _path: config,
        diff_hasher=lambda *_args: "sha256:" + "c" * 64,
        config_hasher=lambda _path: "sha256:" + "d" * 64,
        schema_fingerprinter=lambda: "sha256:" + "e" * 64,
    )
    request = ModelReceiptGeneratorInput(
        config_path=str(tmp_path / ".omnigate.yaml"),
        repo_path=str(tmp_path),
        repository_id="123",
        base_sha="a" * 40,
        head_sha="b" * 40,
        commit_sha="b" * 40,
        branch="feature",
        checks=(
            {"name": "lint", "command": "ruff", "status": "PASS", "duration_ms": 10},
        ),
        sign=False,
    )
    frozen_time = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    with patch.object(handler_receipt_generator, "datetime", wraps=datetime) as clock:
        clock.now.return_value = frozen_time
        first = await handler.handle(UUID(int=1), request)
        second = await handler.handle(UUID(int=2), request)

    assert first == second
    assert first.receipt_json == second.receipt_json
    assert json.loads(first.receipt_json) == first.receipt
    assert first.receipt["timestamp"] == "2026-10-03T12:00:00Z"
    assert first.receipt["schema_version"]["major"] == 1
    assert first.receipt["checks"] == [
        {**request.checks[0], "stdout_preview": None, "stdout_hash": None}
    ]
    assert first.signed is False
