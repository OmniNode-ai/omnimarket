# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic provision dry run effect: counts the live topics under the provision rule, read-only.

It reads the contract tree and lists the broker's topics. It creates, alters and
deletes nothing. Run it with `python -m omnimarket.nodes.node_topic_provision_dry_run_effect`.
"""

from omnimarket.nodes.node_topic_provision_dry_run_effect.handlers.handler_topic_provision_dry_run import (
    HandlerTopicProvisionDryRun,
)


class NodeTopicProvisionDryRunEffect(HandlerTopicProvisionDryRun):
    """ONEX entry-point wrapper for HandlerTopicProvisionDryRun."""


__all__ = ["HandlerTopicProvisionDryRun", "NodeTopicProvisionDryRunEffect"]
