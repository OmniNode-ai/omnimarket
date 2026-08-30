# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_projection_work_events -- the L1 work-ledger projection (OMN-16180).

Re-exports the handler and its models so the package named by this node's
``onex.nodes`` entry point actually resolves them.

This is also the node's wiring evidence for the OMN-10821 unimported-handler
check. The sibling ``node_projection_session_replay`` satisfies that check only
through a test module under ``src/`` -- but OMN-14338 established that a
node-local ``src/omnimarket/nodes/<node>/tests/`` directory is NEVER collected
by CI, so this node's tests live under ``tests/unit/nodes/`` where they actually
run. A real package export is therefore the honest way to evidence wiring here:
it is load-bearing at import time rather than an artifact of where tests happen
to sit.
"""

from omnimarket.nodes.node_projection_work_events.handlers.handler_projection_work_events import (
    HandlerProjectionWorkEvents,
)
from omnimarket.nodes.node_projection_work_events.ledger_view import (
    parse_ledger_view,
    render_ledger_view,
    rows_from_records,
)
from omnimarket.nodes.node_projection_work_events.models.model_work_event import (
    EnumActorKind,
    EnumWorkEventKind,
    ModelProjectionWorkEventsResult,
    ModelWorkEventInbound,
    ModelWorkEventRow,
    WorkEventProjectionError,
)

__all__: list[str] = [
    "EnumActorKind",
    "EnumWorkEventKind",
    "HandlerProjectionWorkEvents",
    "ModelProjectionWorkEventsResult",
    "ModelWorkEventInbound",
    "ModelWorkEventRow",
    "WorkEventProjectionError",
    "parse_ledger_view",
    "render_ledger_view",
    "rows_from_records",
]
