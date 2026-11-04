# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator access mode and the supervision rules it switches (OMN-20892 to OMN-20896)."""


def check_operator_reply_for_phone(text: str) -> str:
    """Public node boundary for an answer sent outside an interactive session.

    This runs the reply check without applying the session's attention push gate.
    An empty result allows the reply; otherwise it is the check's refusal reason.
    """
    from .handlers.handler_operator_reply_check import HandlerOperatorReplyCheck
    from .models.enum_access_mode import EnumAccessMode
    from .models.model_operator_reply_request import ModelOperatorReplyRequest

    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(text=text, mode=EnumAccessMode.REMOTE, to_phone=True)
    )
    return "" if verdict.allowed else verdict.reason
