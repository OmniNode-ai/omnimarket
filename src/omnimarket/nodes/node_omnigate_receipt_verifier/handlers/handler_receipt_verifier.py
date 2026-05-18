# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for the OmniGate receipt verifier node."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from omnimarket.nodes.node_omnigate_receipt_verifier.models.model_receipt_verifier_input import (
    ModelReceiptVerifierInput,
    ModelReceiptVerifierResult,
)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["compute"]
Verifier = Callable[
    [str, Path, Path, str, str, str, str, str | None],
    dict[str, object],
]


def _verify_pr_receipt(
    pr_body: str,
    repo_path: Path,
    config_path: Path,
    repository_id: str,
    repository_url: str,
    base_sha: str,
    head_sha: str,
    actor: str | None,
) -> dict[str, object]:
    from omnibase_infra.gate.action_verify import verify_pr_receipt

    return cast(
        "dict[str, object]",
        verify_pr_receipt(
            pr_body,
            repo_path,
            config_path,
            repository_id=repository_id,
            repository_url=repository_url,
            base_sha=base_sha,
            head_sha=head_sha,
            actor=actor,
        ),
    )


class HandlerReceiptVerifier:
    """Compute handler that delegates to the read-only OmniGate verifier."""

    def __init__(self, *, verifier: Verifier | None = None) -> None:
        self._verifier = verifier or _verify_pr_receipt

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "compute"

    async def handle(
        self,
        correlation_id: UUID,
        request: ModelReceiptVerifierInput,
    ) -> ModelReceiptVerifierResult:
        _ = correlation_id
        decision = self._verifier(
            request.pr_body,
            Path(request.repo_path),
            Path(request.config_path),
            request.repository_id,
            request.repository_url,
            request.base_sha,
            request.head_sha,
            request.actor,
        )
        return ModelReceiptVerifierResult.model_validate(decision)


__all__ = ["HandlerReceiptVerifier"]
