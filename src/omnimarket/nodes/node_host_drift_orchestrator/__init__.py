# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host drift orchestrator node (OMN-24197).

On a runtime tick it sends the host drift check to each declared lab host as one lab work unit and
publishes one run result with one verdict per host.
"""
