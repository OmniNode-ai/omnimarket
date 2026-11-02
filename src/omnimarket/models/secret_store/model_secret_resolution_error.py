# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed refusals of a secret reference that cannot be resolved (OMN-12824, OMN-21921).

Held here, below both the inference resolver and the secret store node, so the
node can raise a refusal the resolver's callers already catch without importing
the resolver.
"""

from __future__ import annotations


class SecretResolutionError(RuntimeError):
    """Raised when a declared ``api_key_ref`` cannot be resolved fail-closed."""


class SecretStoreRefusedError(SecretResolutionError):
    """The machine's secret store answered a reference with something other than a value.

    Carries the store's outcome name (``unreachable``, ``refused``,
    ``misconfigured``...) and a detail that names fields, files and exception
    types only. Distinct from a store that simply holds no value, which the
    resolver reports as a reference to register: a store that cannot be asked
    is not the customer's to fill.
    """

    def __init__(self, message: str, *, outcome: str) -> None:
        super().__init__(message)
        self.outcome = outcome


__all__ = ["SecretResolutionError", "SecretStoreRefusedError"]
