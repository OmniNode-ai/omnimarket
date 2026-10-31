# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The caller's session model, the savings baseline it names (OMN-20557).

Ruling D3 makes a run's savings baseline the developer's own session model when
it is known. The /onex:delegate plugin knows it: Claude Code names the
session's model, the plugin passes it to ``onex delegate --session-model``, and
the CLI writes it into the request's ``metadata`` map under
:data:`SESSION_MODEL_METADATA_KEY`. ``metadata`` is the map every released
request consumer already accepts, so an older consumer ignores the key rather
than refusing the request.

The handler resolves the receipt's baseline from it, and passes it to a
dispatch port that declares ``session_model`` so the port's own evidence row
and saving are stated against the same baseline. A port that does not declare
it (omnibase_infra's runtime port) never receives the keyword; its reported
saving was priced against another baseline, so the handler drops it and states
the saving itself against the session model.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

__all__ = [
    "SESSION_MODEL_METADATA_KEY",
    "drop_saving_priced_elsewhere",
    "request_session_model",
    "session_model_dispatch_kwargs",
]

#: The request ``metadata`` key carrying the caller's session model.
SESSION_MODEL_METADATA_KEY = "session_model"


#: ``{"session_model": ...}`` or empty. Typed loosely on purpose: the
#: handler's dispatch Protocol does not declare the keyword, because released
#: runtime ports do not take it; only a port that declares it receives it.
SessionModelDispatchKwargs = dict[str, Any]


def request_session_model(request: ModelDelegateSkillRequest) -> str | None:
    """The session model the caller named, or None when it named none."""
    value = request.metadata.get(SESSION_MODEL_METADATA_KEY, "").strip()
    return value or None


def session_model_dispatch_kwargs(
    request: ModelDelegateSkillRequest, dispatch: Callable[..., object]
) -> SessionModelDispatchKwargs:
    """``{"session_model": ...}`` only when named and the port declares it."""
    session_model = request_session_model(request)
    if session_model is None:
        return {}
    try:
        parameters = inspect.signature(dispatch).parameters
    except (TypeError, ValueError):
        return {}
    accepts = "session_model" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    return {"session_model": session_model} if accepts else {}


def drop_saving_priced_elsewhere(
    result: dict[str, object],
    request: ModelDelegateSkillRequest,
    passed: SessionModelDispatchKwargs,
) -> dict[str, object]:
    """``result`` without a saving the port priced against another baseline.

    When the request names a session model the port never received, the port's
    ``cost_savings_usd`` is stated against the port's own baseline. Keeping it
    would put that number under the session model's name on the receipt.
    """
    if request_session_model(request) is None or passed:
        return result
    return {key: value for key, value in result.items() if key != "cost_savings_usd"}
