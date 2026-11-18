# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the duplicate-fix guard reads pull requests the PR watcher state does not hold (OMN-22654, OMN-23634)."""

from typing import Protocol

from omnimarket.models.model_github_gateway_io import (
    ModelMergedPrResult,
    ModelOpenPrFactsResult,
)


class OpenPrFactsUnreadableError(Exception):
    """The source could not give a complete list of the open pull requests; the message says why."""


class ProtocolOpenPrFactsSource(Protocol):
    def read(self, repo: str) -> ModelOpenPrFactsResult:
        """Every open pull request of ``repo`` (``owner/name``), or ``OpenPrFactsUnreadableError``."""
        ...

    def read_pr(self, repo: str, number: int) -> ModelMergedPrResult:
        """One pull request of ``repo`` in any state, closed ones included, or ``OpenPrFactsUnreadableError``."""
        ...
