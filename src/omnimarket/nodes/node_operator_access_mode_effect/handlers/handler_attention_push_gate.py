# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Push the operator only for something that needs them (OMN-20896).

Operator, 2026-10-10: "I only want push notifications if there's something that needs my
attention." A push must open with one of the attention kinds below, so the phone shows what to act
on first; routine status, progress and "done" reports are never pushed.

* ``DECISION:`` an open decision only the operator can make;
* ``APPROVAL:`` an approval or consent a gate is waiting on;
* ``CREDENTIAL:`` a credential, login or key that has to be supplied;
* ``RESTORE FAILED:`` a failed restore;
* ``M4 RED:`` a red M4 board check that no lane owns;
* ``LAB DOWN:`` a lab outage.

At the terminal (LOCAL) the same item is shown in the session instead, unless the operator has
been idle for 30 minutes (no prompt). REMOTE pushes it. The push text is also held to the reply
check (OMN-20894): no bare ids, and no local paths, since a push always lands on the phone.
"""

from __future__ import annotations

from ..models.enum_access_mode import EnumAccessMode
from ..models.model_hook_verdict import ModelHookVerdict
from ..models.model_operator_reply_request import ModelOperatorReplyRequest
from ..models.model_push_gate_request import ModelPushGateRequest
from .handler_operator_reply_check import HandlerOperatorReplyCheck

ATTENTION_KINDS = (
    "DECISION:",
    "APPROVAL:",
    "CREDENTIAL:",
    "RESTORE FAILED:",
    "M4 RED:",
    "LAB DOWN:",
)
IDLE_PUSH_SECONDS = 30 * 60
PUSH_GUARD = "operator_access_mode_push_gate"
PUSH_HEAD = "BLOCKED (attention-only push, OMN-20896): "


def attention_kind(message: str) -> str:
    head = message.lstrip().upper()
    for kind in ATTENTION_KINDS:
        if head.startswith(kind):
            return kind
    return ""


class HandlerAttentionPushGate:
    """Refuse a push that is routine, or redundant because the operator is at the terminal."""

    def handle(self, request: ModelPushGateRequest) -> ModelHookVerdict:
        if not attention_kind(request.message):
            return ModelHookVerdict.refuse(
                PUSH_HEAD
                + "this push is not an attention item. Push only for "
                + ", ".join(k.rstrip(":") for k in ATTENTION_KINDS)
                + ", with that word first (for example 'DECISION: ...'). Routine status,"
                " progress and completions go in the session reply, never in a push.",
                PUSH_GUARD,
                "not-an-attention-item",
            )
        if (
            request.mode is EnumAccessMode.LOCAL
            and request.idle_seconds is not None
            and request.idle_seconds < IDLE_PUSH_SECONDS
        ):
            minutes = int(request.idle_seconds // 60)
            return ModelHookVerdict.refuse(
                PUSH_HEAD
                + f"the operator is at the terminal (last prompt {minutes} min ago): put this"
                " item at the top of the session reply instead. A push goes out at the terminal"
                " only after 30 minutes without a prompt.",
                PUSH_GUARD,
                "operator-at-terminal",
            )
        reply = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(request.message, request.mode, to_phone=True)
        )
        if not reply.allowed:
            return reply
        return ModelHookVerdict.allow()
