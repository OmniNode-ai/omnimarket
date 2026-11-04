# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative compute node and compatibility entry point for scope derivation."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import ModelCodeEditScopeRequest, ModelCodeEditScopeResult

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeCodeEditScopeCompute:
    contract_path = CONTRACT

    def handle(self, request: ModelCodeEditScopeRequest) -> ModelCodeEditScopeResult:
        from .handler import HandlerCodeEditScope

        return HandlerCodeEditScope().handle(request)


def derive_scope(
    task: str, tracked_files: Sequence[str], python: str
) -> tuple[list[str], list[str]]:
    from .models import ModelCodeEditScopeRequest

    result = NodeCodeEditScopeCompute().handle(
        ModelCodeEditScopeRequest(task=task, tracked_files=tracked_files, python=python)
    )
    return result.writable, result.checks
