# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-admission effect: decides and logs what it would refuse at each start path.

``HandlerStartAdmission.handle(ModelStartAdmissionRequest) -> ModelStartAdmissionResult``; the gate
store and the log are injected and the ``start_admission`` overlay sets each path's mode.
"""

from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
)

__all__ = ["HandlerStartAdmission"]
