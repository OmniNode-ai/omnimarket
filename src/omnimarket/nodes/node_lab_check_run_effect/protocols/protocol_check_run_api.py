# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of the node's two seams: the check-run API and the JSON HTTP transport."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    ModelCheckRunRecord,
)


class CheckRunApiError(RuntimeError):
    """The check-run API refused, failed or could not be reached; never carries a token."""


class ProtocolCheckRunApi(Protocol):
    @property
    def calls(self) -> int: ...

    def list_runs(
        self, *, repository: str, head_sha: str, check_name: str
    ) -> tuple[ModelCheckRunRecord, ...]: ...

    def create_run(
        self,
        *,
        repository: str,
        head_sha: str,
        name: str,
        attempt_id: str,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord: ...

    def update_run(
        self,
        *,
        repository: str,
        check_run_id: int,
        status: str,
        conclusion: str | None,
        title: str,
        summary: str,
    ) -> ModelCheckRunRecord: ...


class ProtocolHttpJson(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout: float,
    ) -> tuple[int, bytes]: ...


__all__ = ["CheckRunApiError", "ProtocolCheckRunApi", "ProtocolHttpJson"]
