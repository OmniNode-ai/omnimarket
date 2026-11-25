# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one read the record source needs of the repository gateway node."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_github_gateway_io import (
    GithubGatewayResult,
    ModelGithubGatewayRequest,
)


class ProtocolGatewayRead(Protocol):
    def read(self, request: ModelGithubGatewayRequest) -> GithubGatewayResult:
        """One typed gateway answer; raises ``GatewayReadError`` when none comes back."""
        ...
