# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_open_obligations -- "what is currently owed" (OMN-17019).

Re-exports the handler, its models and the renderers so the package named by
this node's ``onex.nodes`` entry point actually resolves them.

This is also the node's wiring evidence for the OMN-10821 unimported-handler
check. A real package export is load-bearing at import time, rather than an
artifact of where tests happen to sit -- the same reasoning the sibling
node_projection_work_events records in its own ``__init__``.
"""

from omnimarket.nodes.node_projection_open_obligations.handlers.handler_projection_open_obligations import (
    HandlerProjectionOpenObligations,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_obligation_event import (
    CLOSED_STATE_BY_KIND,
    REQUIRED_FIELDS_BY_KIND,
    TERMINAL_KINDS,
    EnumActorKind,
    EnumObligationEventKind,
    EnumObligationState,
    ModelObligationEventInbound,
    ModelOpenObligationRow,
    ModelProjectionOpenObligationsResult,
    ObligationProjectionError,
)
from omnimarket.nodes.node_projection_open_obligations.models.model_open_obligation_view import (
    ModelOpenObligationView,
)
from omnimarket.nodes.node_projection_open_obligations.obligations_view import (
    open_obligations,
    parse_open_obligations,
    render_delivery_acknowledgements,
    render_open_obligations,
    rows_from_records,
    unmet_obligations_for_close,
)

__all__: list[str] = [
    "CLOSED_STATE_BY_KIND",
    "REQUIRED_FIELDS_BY_KIND",
    "TERMINAL_KINDS",
    "EnumActorKind",
    "EnumObligationEventKind",
    "EnumObligationState",
    "HandlerProjectionOpenObligations",
    "ModelObligationEventInbound",
    "ModelOpenObligationRow",
    "ModelOpenObligationView",
    "ModelProjectionOpenObligationsResult",
    "ObligationProjectionError",
    "open_obligations",
    "parse_open_obligations",
    "render_delivery_acknowledgements",
    "render_open_obligations",
    "rows_from_records",
    "unmet_obligations_for_close",
]
