# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five handlers of the handler-shape slice take one typed request (OMN-21787)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from omnibase_core.validation.validator_handler_shape import ValidatorHandlerShape
from pydantic import BaseModel

from omnimarket.nodes.node_contract_reducer.handlers.handler_contract_reducer import (
    HandlerContractReducer,
)
from omnimarket.nodes.node_emit_daemon.handlers.handler_emit_daemon import (
    HandlerEmitDaemon,
)
from omnimarket.nodes.node_log_projection.handlers.handler_log_projection import (
    NodeLogProjection,
)
from omnimarket.nodes.node_loop_state_reducer.handlers.handler_loop_state import (
    HandlerLoopState,
)
from omnimarket.nodes.node_omnigate_projection.handlers.handler_omnigate_projection import (
    HandlerOmniGateProjection,
)

pytestmark = pytest.mark.unit

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SLICE: dict[str, type] = {
    "node_contract_reducer/handlers/handler_contract_reducer.py": HandlerContractReducer,
    "node_emit_daemon/handlers/handler_emit_daemon.py": HandlerEmitDaemon,
    "node_log_projection/handlers/handler_log_projection.py": NodeLogProjection,
    "node_loop_state_reducer/handlers/handler_loop_state.py": HandlerLoopState,
    "node_omnigate_projection/handlers/handler_omnigate_projection.py": HandlerOmniGateProjection,
}


@pytest.mark.parametrize("relative", sorted(_SLICE))
def test_slice_handler_has_no_handler_shape_finding(relative: str) -> None:
    path = _NODES / relative
    assert path.is_file(), path
    result = ValidatorHandlerShape().validate([path])
    assert result.metadata is not None
    assert result.metadata.files_processed >= 1
    assert [issue.message for issue in result.issues] == []


@pytest.mark.parametrize("relative", sorted(_SLICE))
def test_slice_handle_takes_one_typed_request_and_returns_a_model(
    relative: str,
) -> None:
    handler_class = _SLICE[relative]
    hints = inspect.get_annotations(handler_class.handle, eval_str=True)
    parameters = [name for name in hints if name != "return"]
    assert len(parameters) == 1
    request = hints[parameters[0]]
    assert isinstance(request, type)
    assert issubclass(request, BaseModel)
    returned = hints["return"]
    members = getattr(returned, "__args__", (returned,))
    assert all(issubclass(member, BaseModel) for member in members)
