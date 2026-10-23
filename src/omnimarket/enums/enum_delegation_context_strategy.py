# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a delegation attempt's context was assembled (OMN-20168)."""

from enum import StrEnum


class EnumDelegationContextStrategy(StrEnum):
    """The context strategy an attempt was sent with.

    full_context: the request's prompt and context as the caller sent them.
    retrieved_excerpts: excerpts retrieved from the sources instead of the whole.
    computed_facts: facts computed in code first, then handed to the model.
    task_restated: the task restated before it was sent.
    """

    FULL_CONTEXT = "full_context"
    RETRIEVED_EXCERPTS = "retrieved_excerpts"
    COMPUTED_FACTS = "computed_facts"
    TASK_RESTATED = "task_restated"
