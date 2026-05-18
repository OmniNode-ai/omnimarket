# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerReceiptVerifier."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from omnimarket.nodes.node_omnigate_receipt_verifier.handlers.handler_receipt_verifier import (
    HandlerReceiptVerifier,
)
from omnimarket.nodes.node_omnigate_receipt_verifier.models.model_receipt_verifier_input import (
    ModelReceiptVerifierInput,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("00000000-0000-4000-a000-000000000145")


@pytest.mark.asyncio
async def test_receipt_verifier_returns_fixed_decision(tmp_path: Path) -> None:
    calls: list[tuple[str, Path, Path, str | None]] = []

    def verify(
        pr_body: str,
        repo_path: Path,
        config_path: Path,
        repository_id: str,
        repository_url: str,
        base_sha: str,
        head_sha: str,
        actor: str | None,
    ) -> dict[str, object]:
        assert repository_id == "123"
        assert repository_url == "https://github.com/org/repo"
        assert base_sha == "a" * 40
        assert head_sha == "b" * 40
        calls.append((pr_body, repo_path, config_path, actor))
        return {
            "ok": True,
            "action": "pass",
            "reason": "ok",
            "receipt_diff_hash": "sha256:" + "c" * 64,
            "checked_at": "2026-05-17T00:00:00Z",
        }

    request = ModelReceiptVerifierInput(
        pr_body="body",
        repo_path=str(tmp_path),
        config_path=str(tmp_path / ".omnigate.yaml"),
        repository_id="123",
        repository_url="https://github.com/org/repo",
        base_sha="a" * 40,
        head_sha="b" * 40,
        actor="contributor",
    )

    result = await HandlerReceiptVerifier(verifier=verify).handle(
        _CORRELATION_ID,
        request,
    )

    assert result.ok is True
    assert result.action == "pass"
    assert result.receipt_diff_hash == "sha256:" + "c" * 64
    assert calls == [("body", tmp_path, tmp_path / ".omnigate.yaml", "contributor")]
