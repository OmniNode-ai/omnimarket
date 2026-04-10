# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_redeploy.

These topics belong to the deploy agent's command/event contract.
See docs/plans/2026-04-03-deploy-agent-sidecar-design.md for the full schema.
"""

from __future__ import annotations

# Command topic — published to trigger a deploy agent rebuild
TOPIC_DEPLOY_REBUILD_REQUESTED = "onex.cmd.deploy.rebuild-requested.v1"

# Event topic — emitted by the deploy agent when a rebuild completes
TOPIC_DEPLOY_REBUILD_COMPLETED = "onex.evt.deploy.rebuild-completed.v1"

__all__: list[str] = [
    "TOPIC_DEPLOY_REBUILD_COMPLETED",
    "TOPIC_DEPLOY_REBUILD_REQUESTED",
]
