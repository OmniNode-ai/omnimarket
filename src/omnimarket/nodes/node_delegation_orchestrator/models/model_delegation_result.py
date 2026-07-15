# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Compatibility import for the canonical delegation result DTO.

OMN-14600: also re-exports the two thin terminal-outcome subclasses
(``ModelDelegationCompleted`` / ``ModelDelegationFailed``) the orchestrator's
``_emit_terminal`` constructs directly — class identity alone is what
class-name -> topic routing uses to disambiguate the completed vs failed
terminal (replacing the earlier bespoke envelope carrier).
"""

from omnibase_core.models.delegation.wire import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)

__all__: list[str] = [
    "ModelDelegationCompleted",
    "ModelDelegationFailed",
    "ModelDelegationResult",
]
