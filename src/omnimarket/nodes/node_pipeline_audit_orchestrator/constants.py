# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Named constants for node_pipeline_audit_orchestrator [OMN-13693].

Topic-namespace prefixes are declared here so handler code never contains
bare string literals that could silently diverge from the platform convention.
"""

from __future__ import annotations

#: Prefix shared by all dead-letter queue topics in the ONEX platform.
#: Topics matching this prefix are excluded from "produced but no consumer"
#: findings because DLQ consumers are optional by design.
DLQ_TOPIC_PREFIX: str = "onex.dlq."
