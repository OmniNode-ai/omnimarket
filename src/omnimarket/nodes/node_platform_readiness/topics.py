# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_platform_readiness.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.
"""

from __future__ import annotations

TOPIC_READINESS_ASSESSED = "onex.evt.platform.readiness-assessed.v1"

__all__: list[str] = ["TOPIC_READINESS_ASSESSED"]
