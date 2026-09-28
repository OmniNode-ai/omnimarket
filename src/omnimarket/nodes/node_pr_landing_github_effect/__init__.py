# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_landing_github_effect: the PR landing workflow's GitHub effect.

OMN-19826 froze the seam (contract, models, transport protocol, response
classification); OMN-19831 added the handler and contract 1.1.0. The wave-3
compose (OMN-19829, contract 1.2.0) registers its ``onex.nodes`` entry point
and declares its event_bus and handler_routing beside the orchestrator that
publishes its command.
"""
