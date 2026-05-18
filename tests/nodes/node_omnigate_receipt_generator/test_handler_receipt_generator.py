# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerReceiptGenerator."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from omnimarket.nodes.node_omnigate_receipt_generator.handlers.handler_receipt_generator import (
    HandlerReceiptGenerator,
)
from omnimarket.nodes.node_omnigate_receipt_generator.models.model_receipt_generator_input import (
    ModelReceiptGeneratorInput,
)

pytestmark = pytest.mark.unit

_CORRELATION_ID = UUID("00000000-0000-4000-a000-000000000145")


class _Receipt:
    def __init__(self, payload: dict[str, object], *, signed: bool = False) -> None:
        self.payload = dict(payload)
        if signed:
            self.payload["sigstore_bundle_json"] = "{}"

    def model_dump(self, *, mode: str, by_alias: bool) -> dict[str, object]:
        assert mode == "json"
        assert by_alias is True
        return dict(self.payload)


def _hash_dependencies(
    tmp_path: Path,
    *,
    config: object,
) -> tuple[
    object,
    object,
    object,
    object,
]:
    def load_config(config_path: Path) -> object:
        assert config_path == tmp_path / ".omnigate.yaml"
        return config

    def diff_hash(
        repo_path: Path, base_sha: str, head_sha: str, allow_empty: bool
    ) -> str:
        assert repo_path == tmp_path
        assert base_sha == "a" * 40
        assert head_sha == "b" * 40
        assert allow_empty is False
        return "sha256:" + "c" * 64

    def config_hash(config_path: Path) -> str:
        assert config_path == tmp_path / ".omnigate.yaml"
        return "sha256:" + "d" * 64

    def schema_fingerprint() -> str:
        return "sha256:" + "e" * 64

    return load_config, diff_hash, config_hash, schema_fingerprint


@pytest.mark.asyncio
async def test_receipt_generator_binds_hashes_and_signs(tmp_path: Path) -> None:
    config = SimpleNamespace(
        project_name="Omni",
        project_url="https://github.com/org/repo",
        receipt=SimpleNamespace(signing="sigstore"),
    )
    built_payloads: list[dict[str, object]] = []

    def build_receipt(payload: dict[str, object]) -> _Receipt:
        built_payloads.append(payload)
        return _Receipt(payload)

    def sign(receipt: object) -> _Receipt:
        assert isinstance(receipt, _Receipt)
        return _Receipt(receipt.payload, signed=True)

    load_config, diff_hash, config_hash, schema_fingerprint = _hash_dependencies(
        tmp_path,
        config=config,
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
            {
                "name": "lint",
                "command": "ruff",
                "status": "PASS",
                "duration_ms": 10,
            },
        ),
    )

    result = await HandlerReceiptGenerator(
        config_loader=load_config,
        diff_hasher=diff_hash,
        config_hasher=config_hash,
        schema_fingerprinter=schema_fingerprint,
        receipt_builder=build_receipt,
        signer=sign,
    ).handle(_CORRELATION_ID, request)

    assert result.signed is True
    assert result.diff_hash == "sha256:" + "c" * 64
    assert result.config_hash == "sha256:" + "d" * 64
    assert result.receipt["repository_id"] == "123"
    assert result.receipt["sigstore_bundle_json"] == "{}"
    assert built_payloads[0]["checks"] == list(request.checks)


@pytest.mark.asyncio
async def test_receipt_generator_does_not_sign_when_policy_is_none(
    tmp_path: Path,
) -> None:
    config = SimpleNamespace(
        project_name="Omni",
        project_url="https://github.com/org/repo",
        receipt=SimpleNamespace(signing="none"),
    )
    request = ModelReceiptGeneratorInput(
        config_path=str(tmp_path / ".omnigate.yaml"),
        repo_path=str(tmp_path),
        repository_id="123",
        base_sha="a" * 40,
        head_sha="b" * 40,
        commit_sha="b" * 40,
    )

    def sign(receipt: object) -> _Receipt:
        assert isinstance(receipt, _Receipt)
        return _Receipt({}, signed=True)

    load_config, diff_hash, config_hash, schema_fingerprint = _hash_dependencies(
        tmp_path,
        config=config,
    )
    result = await HandlerReceiptGenerator(
        config_loader=load_config,
        diff_hasher=diff_hash,
        config_hasher=config_hash,
        schema_fingerprinter=schema_fingerprint,
        receipt_builder=_Receipt,
        signer=sign,
    ).handle(_CORRELATION_ID, request)

    assert result.signed is False
    assert "sigstore_bundle_json" not in result.receipt
