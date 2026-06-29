# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Command model for the deploy publish-monitor effect node.

Canonical owner is ``omnimarket.events.runtime_deployment`` — the redeploy
ORCHESTRATOR builds this command and this EFFECT node consumes it, so it is a
shared cross-node type. This module re-exports it from the owner so the node has
a single source of truth without a duplicate definition or a cross-node reach-in.
"""

from __future__ import annotations

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand

__all__: list[str] = ["ModelDeployPublishCommand"]
