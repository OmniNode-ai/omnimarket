# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Empty answer that billed output tokens (OMN-26588)."""

from __future__ import annotations

#: The gate check name recorded on an empty answer that billed output tokens.
EMPTY_AFTER_SPENT_OUTPUT_CHECK_NAME = "answer_present_after_spent_output"


def empty_after_spent_output_gate_failure_reason(output_tokens_spent: int) -> str:
    """The gate failure reason for an empty answer that billed output tokens (OMN-26588).

    A model that reasons by default can spend its whole completion budget on
    thinking and return empty ``content`` under a provider stop reason other than
    ``length``. The ``WEAK_OUTPUT`` prefix, not ``MALFORMED``, because the next
    rung brings its own budget and routinely finishes the answer; ``MALFORMED``
    is a verdict the reducer never escalates on.
    """
    return (
        f"WEAK_OUTPUT: the response is empty after {output_tokens_spent} output "
        "tokens were spent, so the output-token budget went to something other "
        "than the answer (reasoning) and this is not a completed answer"
    )


def is_empty_answer_after_spent_output(content: str, output_tokens_spent: int) -> bool:
    """Whether the answer is empty although the provider billed output tokens.

    An empty answer that billed none carries no evidence the model ran out of
    budget, so it is not this case and keeps its ``MALFORMED`` verdict.
    """
    return not content.strip() and output_tokens_spent > 0
