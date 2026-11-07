# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the duplicate-fix guard reads the open pull requests when the host has no PR watcher state (OMN-22654)."""

from typing import Protocol

from omnimarket.models.model_github_gateway_io import ModelOpenPrFactsResult


class OpenPrFactsUnreadableError(Exception):
    """The source could not give a complete list of the open pull requests; the message says why."""


class ProtocolOpenPrFactsSource(Protocol):
    def read(self, repo: str) -> ModelOpenPrFactsResult:
        """Every open pull request of ``repo`` (``owner/name``), or ``OpenPrFactsUnreadableError``."""
        ...
