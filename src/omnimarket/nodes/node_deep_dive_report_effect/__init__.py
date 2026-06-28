# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_deep_dive_report_effect — daily deep-dive report EFFECT (OMN-13725).

Sole owner of git/gh/Linear I/O for the deep-dive report. All reads route
through an injected ProtocolReportDataSource; pure scoring/rendering reuse
the local deep_dive package (one source of truth with generate_deep_dive.py).
"""

from omnimarket.nodes.node_deep_dive_report_effect.handlers.handler_deep_dive_report_effect import (
    HandlerDeepDiveReportEffect,
)

__all__ = ["HandlerDeepDiveReportEffect"]
