# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How the orchestrator answers an additive call: a client verdict (OMN-22016, plan task TC.11).

A Claude Code hook that asks for a package is answered with the action that ends its
receipt-mode wait and the invocation id that joins the reply to it. A profile the server-side
list does not enable is never built and is answered ``none``; a build that fails is answered
``none`` too, so the hook injects nothing. An enabled profile's package is answered ``inject``
with the sha256 of the text it carries, which the hook checks before it prints.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Collection

from omnimarket.models.claude_code_binding import EnumClientVerdictAction
from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackCommand,
)


def answer_additive(
    command: ModelContextPackCommand,
    *,
    enabled: Collection[str],
    build: Callable[[ModelContextPackCommand], ModelContextPackBuilt],
) -> ModelContextPackBuilt:
    """Answer one additive call; ``build`` is the ordinary build of the same command."""
    request = command.request
    if request.profile_name not in enabled:
        return ModelContextPackBuilt(
            status=EnumContextPackOrchestratorStatus.NOT_ENABLED,
            profile_name=request.profile_name,
            action=EnumClientVerdictAction.NONE,
            invocation_id=request.invocation_id,
        )
    built = build(command)
    if built.status is not EnumContextPackOrchestratorStatus.OK:
        return built.model_copy(
            update={
                "action": EnumClientVerdictAction.NONE,
                "invocation_id": request.invocation_id,
            }
        )
    text = "\n\n".join(message.text for message in built.messages)
    return built.model_copy(
        update={
            "action": EnumClientVerdictAction.INJECT,
            "invocation_id": request.invocation_id,
            "text": text,
            "package_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    )
