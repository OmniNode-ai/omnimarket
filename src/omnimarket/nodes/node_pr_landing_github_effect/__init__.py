# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_landing_github_effect: the PR landing workflow's GitHub effect.

OMN-19826 froze the seam (contract, models, transport protocol, response
classification); OMN-19831 added the handler and contract 1.1.0. The node has
no ``onex.nodes`` entry point and no event_bus block, so the runtime does not
wire it until the wave-3 compose step.
"""
