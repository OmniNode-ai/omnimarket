# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Verify a correlated compaction reply (OMN-18531 TA.11, plan section 6 step 6)."""

from __future__ import annotations

from uuid import UUID

from pydantic import ValidationError

from omnimarket.models.claude_code_binding import (
    EnumClientVerdictAction,
    ModelClientBinding,
)
from omnimarket.models.context_compaction import (
    ModelContextCompactionPackageReply,
    installed_messages_hash,
    installed_messages_json,
)
from omnimarket.nodes.node_claude_code_hook_adapter_effect.models import (
    ModelClientVerdict,
    ModelReceiptEvent,
)

COMPACTION_VERDICT_COMPUTE = "context_compaction_verdict"


def compute_compaction_verdict(
    binding: ModelClientBinding,
    events: tuple[ModelReceiptEvent, ...],
    invocation_id: UUID,
) -> ModelClientVerdict:
    """Install only a valid reply for this invocation whose messages match its hash."""
    fallback = ModelClientVerdict(action=binding.unverified_action)
    verdicts = [event.payload for event in events if "action" in event.payload]
    if not verdicts:
        return fallback
    try:
        reply = ModelContextCompactionPackageReply.model_validate(verdicts[-1])
    except ValidationError:
        return fallback
    if reply.invocation_id != invocation_id:
        return fallback
    if reply.action is EnumClientVerdictAction.NATIVE:
        return ModelClientVerdict(action=EnumClientVerdictAction.NATIVE)
    echoed = reply.installed_messages_hash
    if echoed is None or installed_messages_hash(reply.messages) != echoed:
        return fallback
    return ModelClientVerdict(
        action=EnumClientVerdictAction.INSTALL,
        text=installed_messages_json(reply.messages),
        package_sha256=echoed,
    )


__all__ = ["COMPACTION_VERDICT_COMPUTE", "compute_compaction_verdict"]
