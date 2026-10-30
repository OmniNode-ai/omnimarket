# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""WIP admission: admit or refuse the start of work against the started-but-unlanded count.

``HandlerWipAdmission.handle(ModelWipAdmissionRequest) -> ModelWipAdmissionDecision``;
the rule is ``omnimarket.handlers.rules_wip_admission``.
"""

from omnimarket.nodes.node_wip_admission_compute.handlers.handler_wip_admission import (
    HandlerWipAdmission,
)

__all__ = ["HandlerWipAdmission"]
