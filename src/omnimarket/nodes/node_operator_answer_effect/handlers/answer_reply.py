# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compose a read-only reply writer with the existing node's public reply check."""

from collections.abc import Callable

from omnimarket.models.operator_answer import ModelAnswerContext
from omnimarket.nodes.node_operator_access_mode_effect import (
    check_operator_reply_for_phone,
)


class CheckedAnswerWriter:
    def __init__(self, writer: Callable[[ModelAnswerContext], str]) -> None:
        self._writer = writer

    def write(self, context: ModelAnswerContext) -> str:
        return self._writer(context)

    def check(self, text: str) -> str:
        return check_operator_reply_for_phone(text)
