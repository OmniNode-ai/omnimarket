# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for node_developer_lane_admission_compute.

The prompt hook loads ``handler_developer_lane_admission`` by file path, not through this
package; see its docstring.
"""

from omnimarket.nodes.node_developer_lane_admission_compute.handlers.handler_developer_lane_admission import (
    HandlerDeveloperLaneAdmission,
    admit,
)

__all__ = ["HandlerDeveloperLaneAdmission", "admit"]
