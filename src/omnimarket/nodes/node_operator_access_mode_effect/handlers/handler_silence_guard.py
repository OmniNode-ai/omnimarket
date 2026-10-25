# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Silent turns emit only single-line attention items (OMN-20894)."""

from __future__ import annotations

import re

from ..models.model_hook_verdict import ModelHookVerdict
from ..models.model_silence_request import ModelSilenceRequest

SILENCE_PREFIXES = (
    "DECISION",
    "APPROVAL",
    "CREDENTIAL",
    "RESTORE FAILED",
    "M4 RED",
    "LAB DOWN",
)
SILENCE_PATTERN = re.compile(r"^(?:" + "|".join(SILENCE_PREFIXES) + r")\b")
SILENCE_GUARD = "operator_access_mode_silence_guard"


def is_silence_tick(prompt: str) -> bool:
    return prompt.lstrip().startswith("Silence tick")


class HandlerSilenceGuard:
    """Refuse prose in a silent turn, including text emitted before a tool call."""

    def handle(self, request: ModelSilenceRequest) -> ModelHookVerdict:
        if not request.silent:
            return ModelHookVerdict.allow()
        for text in request.texts:
            stripped = text.strip()
            if stripped and (
                len(stripped.splitlines()) != 1 or not SILENCE_PATTERN.match(stripped)
            ):
                return ModelHookVerdict.refuse(
                    "BLOCKED (silent turn, OMN-20894): this turn is silent. Only one-line"
                    " attention items starting with "
                    + ", ".join(SILENCE_PREFIXES)
                    + " are allowed. Otherwise end the turn with no text.",
                    SILENCE_GUARD,
                    "silent-turn-text",
                )
        return ModelHookVerdict.allow()
