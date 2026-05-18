# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for the OmniGate receipt generator node."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from omnimarket.nodes.node_omnigate_receipt_generator.models.model_receipt_generator_input import (
    ModelReceiptGeneratorInput,
    ModelReceiptGeneratorResult,
)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["compute"]
ConfigLoader = Callable[[Path], object]
DiffHasher = Callable[[Path, str, str, bool], str]
ConfigHasher = Callable[[Path], str]
SchemaFingerprinter = Callable[[], str]
ReceiptBuilder = Callable[[dict[str, object]], object]
Signer = Callable[[object], object]


def _load_config(config_path: Path) -> object:
    module = import_module("omnibase_core.gate.config_loader")

    return cast(Any, module).load_omnigate_config(config_path)


def _compute_diff_hash(
    repo_path: Path,
    base_sha: str,
    head_sha: str,
    allow_empty: bool,
) -> str:
    module = import_module("omnibase_core.gate.diff_hash")

    return str(
        cast(Any, module).compute_pr_diff_hash(
            repo_path,
            base_sha=base_sha,
            head_sha=head_sha,
            allow_empty=allow_empty,
        )
    )


def _compute_config_hash(config_path: Path) -> str:
    module = import_module("omnibase_core.gate.diff_hash")

    return str(cast(Any, module).compute_config_hash(config_path))


def _compute_schema_fingerprint() -> str:
    module = import_module("omnibase_core.gate.receipt_canonical")

    return str(cast(Any, module).compute_receipt_schema_fingerprint())


def _build_receipt(payload: dict[str, object]) -> object:
    module = import_module("omnibase_core.models.gate.model_omnigate_receipt")

    return cast(Any, module).ModelOmniGateReceipt.model_validate(payload)


def _sign_receipt(receipt: object) -> object:
    from omnibase_infra.gate.signer import OmniGateSigner

    return OmniGateSigner().sign(receipt)


def _json_dict(model: object) -> dict[str, object]:
    raw = cast(Any, model).model_dump(mode="json", by_alias=True)
    if not isinstance(raw, dict):
        msg = "OmniGate receipt model_dump did not return a mapping"
        raise TypeError(msg)
    return {str(key): value for key, value in raw.items()}


def _json_string(data: dict[str, object]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _receipt_policy_signing(config: object) -> str:
    receipt_policy = getattr(config, "receipt", None)
    return str(getattr(receipt_policy, "signing", "none"))


class HandlerReceiptGenerator:
    """Compute handler that builds and optionally signs an OmniGate receipt."""

    def __init__(
        self,
        *,
        config_loader: ConfigLoader | None = None,
        diff_hasher: DiffHasher | None = None,
        config_hasher: ConfigHasher | None = None,
        schema_fingerprinter: SchemaFingerprinter | None = None,
        receipt_builder: ReceiptBuilder | None = None,
        signer: Signer | None = None,
    ) -> None:
        self._config_loader = config_loader or _load_config
        self._diff_hasher = diff_hasher or _compute_diff_hash
        self._config_hasher = config_hasher or _compute_config_hash
        self._schema_fingerprinter = schema_fingerprinter or _compute_schema_fingerprint
        self._receipt_builder = receipt_builder or _build_receipt
        self._signer = signer or _sign_receipt

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "compute"

    async def handle(
        self,
        correlation_id: UUID,
        request: ModelReceiptGeneratorInput,
    ) -> ModelReceiptGeneratorResult:
        _ = correlation_id
        config_path = Path(request.config_path)
        repo_path = Path(request.repo_path)
        config = self._config_loader(config_path)
        diff_hash = self._diff_hasher(
            repo_path,
            request.base_sha,
            request.head_sha,
            request.allow_empty_diff,
        )
        config_hash = self._config_hasher(config_path)
        payload: dict[str, object] = {
            "schema_version": {"major": 1, "minor": 0, "patch": 0},
            "project_name": request.project_name or str(cast(Any, config).project_name),
            "project_url": request.project_url or str(cast(Any, config).project_url),
            "repository_id": request.repository_id,
            "base_sha": request.base_sha,
            "head_sha": request.head_sha,
            "commit_sha": request.commit_sha,
            "diff_hash": diff_hash,
            "config_hash": config_hash,
            "receipt_schema_fingerprint": self._schema_fingerprinter(),
            "branch": request.branch,
            "timestamp": datetime.now(UTC).isoformat(),
            "checks": list(request.checks),
        }
        receipt = self._receipt_builder(payload)
        signed = False
        if request.sign and _receipt_policy_signing(config) == "sigstore":
            receipt = self._signer(receipt)
            signed = True
        receipt_data = _json_dict(receipt)
        return ModelReceiptGeneratorResult(
            receipt=receipt_data,
            receipt_json=_json_string(receipt_data),
            signed=signed,
            diff_hash=diff_hash,
            config_hash=config_hash,
        )


__all__ = ["HandlerReceiptGenerator"]
