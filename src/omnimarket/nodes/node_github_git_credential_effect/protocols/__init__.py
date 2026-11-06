# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The minting port of node_github_git_credential_effect (OMN-22613)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class GitCredentialUnavailableError(RuntimeError):
    """The App credential is not readable here. The message names refs, never values."""


class GitCredentialMintRefusedError(RuntimeError):
    """GitHub refused the mint, or answered with a token wider than asked."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True, repr=False)
class MintedRepositoryToken:
    """A token narrowed to one repository; ``repr`` is off so it never logs."""

    token: str
    expires_at: str
    permissions: dict[str, str]
    repositories: tuple[str, ...] | None


@runtime_checkable
class ProtocolRepositoryTokenMinter(Protocol):
    """Mints one installation token narrowed to one repository and a permission set."""

    async def mint(
        self, *, owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken: ...


__all__ = [
    "GitCredentialMintRefusedError",
    "GitCredentialUnavailableError",
    "MintedRepositoryToken",
    "ProtocolRepositoryTokenMinter",
]
