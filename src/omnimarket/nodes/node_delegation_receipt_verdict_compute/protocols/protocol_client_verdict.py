# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed table dependency supplied at receipt bridge composition."""

from typing import Protocol

from omnimarket.models.delegation.model_delegation_client_verdict import (
    ModelDelegationClientVerdictRequest,
    ModelDelegationClientVerdictResult,
)


class ProtocolClientVerdict(Protocol):
    """The table compute is supplied at composition and performs no transport I/O."""

    def handle(
        self, request: ModelDelegationClientVerdictRequest
    ) -> ModelDelegationClientVerdictResult: ...
