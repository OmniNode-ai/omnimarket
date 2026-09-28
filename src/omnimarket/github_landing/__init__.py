# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared GitHub landing transport (OMN-19831).

Request and response shapes, pure request builders and the one live urllib
transport, imported by node_pr_landing_github_effect and by the older landing
nodes (node_ci_rerun_effect, node_merge_sweep_auto_merge_arm_effect and
node_pr_lifecycle_fix_effect's auto-rebase), so every GitHub landing call has a
single request shape and a single send path.
"""
