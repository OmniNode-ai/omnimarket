# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical node for the internal consumer release-path refusal."""

from omnimarket.handlers.handler_internal_release_path_guard import (
    HandlerInternalReleasePathGuard,
)


class NodeInternalReleasePathGuardCompute(HandlerInternalReleasePathGuard):
    """Declarative entry point; all policy lives in the shared pure handler."""


__all__ = ["NodeInternalReleasePathGuardCompute"]
