# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_pr_landing_github_effect: the PR landing workflow's GitHub effect (OMN-19826).

Seam only: contract, models, the transport protocol and the response
classification. The handlers are wave-2 task OMN-19831, and the node has no
``onex.nodes`` entry point until then, so the runtime does not wire it.
"""
