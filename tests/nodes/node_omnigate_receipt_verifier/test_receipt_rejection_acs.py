# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Receipt rejection ACs through the node's default verifier."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from omnibase_infra.gate import action_verify

from omnimarket.nodes.node_omnigate_receipt_generator.handlers.handler_receipt_generator import (
    HandlerReceiptGenerator,
)
from omnimarket.nodes.node_omnigate_receipt_generator.models.model_receipt_generator_input import (
    ModelReceiptGeneratorInput,
)
from omnimarket.nodes.node_omnigate_receipt_verifier.handlers.handler_receipt_verifier import (
    HandlerReceiptVerifier,
)
from omnimarket.nodes.node_omnigate_receipt_verifier.models.model_receipt_verifier_input import (
    ModelReceiptVerifierInput,
    ModelReceiptVerifierResult,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("00000000-0000-4000-a000-000000000145")
_DIFF_HASH = "sha256:" + "c" * 64
_CONFIG_HASH = "sha256:" + "d" * 64


@pytest.fixture
def trusted_config(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub checkout I/O, retaining receipt parsing and policy validation."""
    config = SimpleNamespace(
        project_name="Omni",
        project_url="https://github.com/org/repo",
        gate=SimpleNamespace(exempt_users=()),
        receipt=SimpleNamespace(
            max_receipt_bytes=65536,
            max_age_minutes=60,
            advisory_blocks=False,
            signing="none",
            allow_unsigned=True,
            identity=SimpleNamespace(
                expected_issuer="https://token.actions.githubusercontent.com",
                allowed_identities=(
                    "https://github.com/org/repo/.github/workflows/ci.yml",
                ),
                allowed_identity_regexes=(),
            ),
        ),
    )
    monkeypatch.setattr(action_verify, "_verify_commit_object", lambda *_args: None)
    monkeypatch.setattr(action_verify, "_load_omnigate_config", lambda _path: config)
    monkeypatch.setattr(
        action_verify, "_compute_pr_diff_hash", lambda *_args, **_kwargs: _DIFF_HASH
    )
    monkeypatch.setattr(
        action_verify, "_compute_config_hash", lambda _path: _CONFIG_HASH
    )
    return config


@pytest_asyncio.fixture
async def generated_receipt(
    trusted_config: SimpleNamespace, tmp_path: Path
) -> dict[str, object]:
    result = await HandlerReceiptGenerator(
        config_loader=lambda _path: trusted_config,
        diff_hasher=lambda *_args: _DIFF_HASH,
        config_hasher=lambda _path: _CONFIG_HASH,
        schema_fingerprinter=lambda: "sha256:" + "e" * 64,
    ).handle(
        _CORRELATION_ID,
        ModelReceiptGeneratorInput(
            config_path=str(tmp_path / ".omnigate.yaml"),
            repo_path=str(tmp_path),
            repository_id="123",
            base_sha="a" * 40,
            head_sha="b" * 40,
            commit_sha="b" * 40,
            branch="feature",
            checks=(
                {
                    "name": "lint",
                    "command": "ruff",
                    "status": "PASS",
                    "duration_ms": 10,
                },
            ),
            sign=False,
        ),
    )
    return result.receipt


async def _verify(
    receipt: dict[str, object], tmp_path: Path
) -> ModelReceiptVerifierResult:
    return await HandlerReceiptVerifier().handle(
        _CORRELATION_ID,
        ModelReceiptVerifierInput(
            pr_body=(
                "<!-- OMNIGATE_RECEIPT_START -->\n"
                + json.dumps(receipt)
                + "\n<!-- OMNIGATE_RECEIPT_END -->"
            ),
            repo_path=str(tmp_path),
            config_path=str(tmp_path / ".omnigate.yaml"),
            repository_id="123",
            repository_url="https://github.com/org/repo",
            base_sha="a" * 40,
            head_sha="b" * 40,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "mutated_value", "reason"),
    [
        ("repository_id", "456", "Repository id mismatch"),
        ("head_sha", "f" * 40, "Receipt commit/base/head SHA mismatch"),
        ("diff_hash", "sha256:" + "f" * 64, "Diff hash mismatch:"),
        (
            "config_hash",
            "sha256:" + "f" * 64,
            "Config hash mismatch against trusted base config",
        ),
    ],
    ids=["repository-authority", "head-binding", "diff-binding", "config-binding"],
)
async def test_ac_verifier_rejects_mutated_receipt_field(
    generated_receipt: dict[str, object],
    tmp_path: Path,
    field: str,
    mutated_value: str,
    reason: str,
) -> None:
    baseline = await _verify(generated_receipt, tmp_path)
    assert baseline.ok is True
    assert baseline.action == "pass"

    result = await _verify({**generated_receipt, field: mutated_value}, tmp_path)

    assert result.ok is False
    assert result.action == "fail"
    assert result.reason.startswith(reason)


@pytest.mark.asyncio
async def test_ac_verifier_rejects_missing_required_signature(
    generated_receipt: dict[str, object],
    trusted_config: SimpleNamespace,
    tmp_path: Path,
) -> None:
    assert generated_receipt["sigstore_bundle_json"] is None
    trusted_config.receipt.signing = "sigstore"
    trusted_config.receipt.allow_unsigned = False

    result = await _verify(generated_receipt, tmp_path)

    assert result.ok is False
    assert result.action == "fail"
    assert result.reason == "Sigstore signature verification failed"


@pytest.mark.asyncio
async def test_ac_verifier_rejects_invalid_schema_version(
    generated_receipt: dict[str, object], tmp_path: Path
) -> None:
    baseline = await _verify(generated_receipt, tmp_path)
    assert baseline.ok is True

    result = await _verify(
        {**generated_receipt, "schema_version": {"major": -1, "minor": 0, "patch": 0}},
        tmp_path,
    )

    assert result.ok is False
    assert result.action == "fail"
    assert result.reason == "Invalid OmniGate receipt: ValidationError"
