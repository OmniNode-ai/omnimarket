# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the iLO setup verb (OMN-22870).

The setup handler owns the decisions; the lab secret store, the certificate fetch, the operator prompts
and the screen go through these ports, which the CLI binds and a test replaces.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import SecretStr


class SetupStoreError(Exception):
    """The lab secret store could not answer. The text names no value and no device answer."""


class ProtocolSetupSecrets(Protocol):
    """The lab secret store, as the setup verb uses it: key names, a read, and a create-only put."""

    def names(self) -> frozenset[str]:
        """The key names in the iLO folder. Never values."""
        ...

    def get(self, key: str) -> SecretStr | None: ...

    def create(self, key: str, value: SecretStr) -> bool:
        """Create ``key``; ``False`` when it already exists (never updated)."""
        ...


# DER bytes of the certificate the iLO at (address, port) presents. Nothing is sent to the device.
FetchCertificate = Callable[[str, int], bytes]
# The hidden prompt for one host's iLO administrator password: (host, admin user). ``None`` when no
# terminal can ask. The adapter asks twice and returns only a value typed the same both times.
PromptAdminPassword = Callable[[str, str], SecretStr | None]
# "Is this host's administrator password the same as the previous host's?": (host, previous host).
# ``False`` when the operator says no or no terminal can ask.
AskSamePassword = Callable[[str, str], bool]
# Shows one weak certificate's fingerprint and asks the operator to confirm it matches the iLO's own
# certificate page: (host, address, SHA-256 as AB:CD:..., serial, RSA bits). ``False`` when the operator
# declines or no terminal can ask.
ConfirmCertificate = Callable[[str, str, str, str, int], bool]
# The visible prompt for an iLO address; ``None`` when no terminal can ask.
PromptAddress = Callable[[str], str | None]
PasswordGenerator = Callable[[], SecretStr]
Emit = Callable[[str], None]
