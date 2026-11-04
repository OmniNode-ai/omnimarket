# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed refusal for a ticket read whose credential the store did not give (OMN-22418)."""

from __future__ import annotations


class TicketQueryCredentialRefusedError(RuntimeError):
    """The secret store did not resolve the project tracker's credential.

    Names the secret reference and the store's outcome, never a value. There is
    no fallback to an environment variable, so this is the whole answer.
    """

    def __init__(self, secret_reference: str, outcome: str, detail: str = "") -> None:
        self.secret_reference = secret_reference
        self.outcome = outcome
        suffix = f" ({detail})" if detail else ""
        super().__init__(
            f"ticket read refused: secret reference {secret_reference} was not "
            f"resolved by the secret store (outcome {outcome}){suffix}; "
            "the key is read from the store only, never from an environment variable"
        )


__all__ = ["TicketQueryCredentialRefusedError"]
