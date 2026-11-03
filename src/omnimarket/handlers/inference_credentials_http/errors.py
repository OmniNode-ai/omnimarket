# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed read-model errors; the list route degrades one, the revoke route none."""

from __future__ import annotations


class CredentialReadModelMissingTableError(RuntimeError):
    """The projection relation does not exist yet."""


class CredentialReadModelNotConfiguredError(RuntimeError):
    """The projection source is not configured."""


class CredentialReadModelUnavailableError(RuntimeError):
    """The projection source is configured and failed."""


class CredentialPortUnboundError(RuntimeError):
    """A port the route needs is not bound; the route refuses, never guesses."""
