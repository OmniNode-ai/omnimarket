# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start admission effect: reserve before a start, consume at the start, observe or refuse.

``HandlerStartAdmission.handle(ModelStartAdmissionRequest) -> ModelStartAdmissionResult``; the gate
store and the log are injected and the ``start_admission`` overlay selects the policy.
"""

from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
)

__all__ = ["HandlerStartAdmission"]
