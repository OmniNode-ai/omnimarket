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

The handler resolves the receipt's baseline from it and hands it to a dispatch
port that reads it, so the port's own evidence row and saving are stated
against the same baseline. It travels in :data:`current_session_model`, a
context variable the handler sets around the dispatch, and not as a keyword of
``dispatch()`` (OMN-21752): omnibase_infra's keyword-parity check reads the
handler's dispatch call with ``ast`` and holds every keyword it can send to the
runtime port's signature, so a keyword the runtime port does not declare turns
every omnibase_infra commit and PR red. A port says it reads the variable with
a true ``reads_session_model`` attribute. A port that does not (omnibase_infra's
runtime port) never sees the model; its reported saving was priced against
another baseline, so the handler drops it and states the saving itself against
the session model.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)

__all__ = [
    "READS_SESSION_MODEL_ATTRIBUTE",
    "SESSION_MODEL_METADATA_KEY",
    "bind_session_model",
    "current_session_model",
    "drop_saving_priced_elsewhere",
    "port_reads_session_model",
    "request_session_model",
]

#: The request ``metadata`` key carrying the caller's session model.
SESSION_MODEL_METADATA_KEY = "session_model"

#: The attribute a dispatch port sets to true when it reads
#: :data:`current_session_model`.
READS_SESSION_MODEL_ATTRIBUTE = "reads_session_model"

#: The caller's session model for the dispatch running in this context. The
#: run's saving, its evidence row and its metering fold are all stated against
#: the baseline it resolves.
current_session_model: ContextVar[str | None] = ContextVar(
    "session_model", default=None
)


def request_session_model(request: ModelDelegateSkillRequest) -> str | None:
    """The session model the caller named, or None when it named none."""
    value = request.metadata.get(SESSION_MODEL_METADATA_KEY, "").strip()
    return value or None


def port_reads_session_model(dispatch_port: object) -> bool:
    """True when the port says it reads :data:`current_session_model`."""
    return getattr(dispatch_port, READS_SESSION_MODEL_ATTRIBUTE, False) is True


def bind_session_model(
    request: ModelDelegateSkillRequest, dispatch_port: object
) -> Token[str | None] | None:
    """Set the context variable when the request names a model the port reads.

    Returns the reset token, or None when nothing was set; the caller resets
    the variable with it once the dispatch has ended.
    """
    session_model = request_session_model(request)
    if session_model is None or not port_reads_session_model(dispatch_port):
        return None
    return current_session_model.set(session_model)


def drop_saving_priced_elsewhere(
    result: dict[str, object],
    request: ModelDelegateSkillRequest,
    delivered: bool,
) -> dict[str, object]:
    """``result`` without a saving the port priced against another baseline.

    When the request names a session model the port never received, the port's
    ``cost_savings_usd`` is stated against the port's own baseline. Keeping it
    would put that number under the session model's name on the receipt.
    """
    if request_session_model(request) is None or delivered:
        return result
    return {key: value for key, value in result.items() if key != "cost_savings_usd"}
