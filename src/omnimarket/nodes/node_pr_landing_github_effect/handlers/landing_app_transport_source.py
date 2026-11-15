# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer App's repository-scoped token, as the landing mutation transport (OMN-23777).

Reuses node_github_git_credential_effect's minter (the writer App's installation
token narrowed to one repository and a permission set). The landing effect asks
for pull request write and contents write, and refuses before any request when:

- the App refs do not resolve here, or the mint is refused: a personal token is
  never a fallback (``landing_identity_not_app``);
- what came back is not an installation token (``landing_identity_not_app``);
- the grant lacks pull request or contents write, or is not narrowed to exactly
  the one repository (``minted_permissions_insufficient``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from pydantic import SecretStr

from omnimarket.github_app_auth import (
    GitHubAppIdentityError,
    assert_is_app_installation_token,
)
from omnimarket.github_landing.github_landing_transport import (
    UrllibGithubLandingTransport,
)
from omnimarket.nodes.node_github_git_credential_effect.handlers.handler_github_git_credential import (
    resolve_policy,
    split_repository,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    GitCredentialMintRefusedError,
    GitCredentialUnavailableError,
    ProtocolRepositoryTokenMinter,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols.app_repository_token_minter import (
    AppRepositoryTokenMinter,
)
from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.nodes.node_pr_landing_github_effect.protocols import (
    LandingIdentityRefusedError,
    ProtocolPrLandingGithubTransport,
)

# What an arm, enqueue, update-branch or disarm needs; metadata read is the
# floor GitHub adds to every token.
LANDING_MINT_PERMISSIONS: Final[Mapping[str, str]] = {
    "pull_requests": "write",
    "contents": "write",
    "metadata": "read",
}
_REQUIRED_WRITES: Final[tuple[str, ...]] = ("pull_requests", "contents")
_IDENTITY_LABEL: Final[str] = "writer-app-installation-token"

TransportFactory = Callable[[SecretStr], ProtocolPrLandingGithubTransport]


class LandingAppTransportSource:
    """Mints the App token per mutation and builds a transport authenticated with it."""

    def __init__(
        self,
        minter: ProtocolRepositoryTokenMinter | None = None,
        *,
        transport_factory: TransportFactory = UrllibGithubLandingTransport,
    ) -> None:
        self._minter = minter
        self._transport_factory = transport_factory

    @property
    def identity(self) -> str:
        return _IDENTITY_LABEL

    def _resolved_minter(self) -> ProtocolRepositoryTokenMinter:
        if self._minter is None:
            self._minter = AppRepositoryTokenMinter(resolve_policy())
        return self._minter

    async def transport_for(self, repository: str) -> ProtocolPrLandingGithubTransport:
        reasons = EnumPrLandingGithubFailureReason
        parts = split_repository(repository)
        if parts is None:
            raise LandingIdentityRefusedError(
                reasons.VALIDATION_FAILED, "the repository must be OWNER/NAME"
            )
        owner, name = parts
        try:
            minted = await self._resolved_minter().mint(
                owner=owner, name=name, permissions=dict(LANDING_MINT_PERMISSIONS)
            )
        except GitCredentialUnavailableError as exc:
            raise LandingIdentityRefusedError(
                reasons.LANDING_IDENTITY_NOT_APP,
                f"the writer App credential is not available here ({exc}); "
                "a personal token is not accepted for landing mutations",
            ) from None
        except GitCredentialMintRefusedError as exc:
            raise LandingIdentityRefusedError(
                reasons.LANDING_IDENTITY_NOT_APP,
                f"the writer App token mint was refused ({exc})",
            ) from None
        try:
            assert_is_app_installation_token(minted.token)
        except GitHubAppIdentityError:
            raise LandingIdentityRefusedError(
                reasons.LANDING_IDENTITY_NOT_APP,
                "the minted credential is not a GitHub App installation token",
            ) from None
        missing = [
            f"{permission}=write"
            for permission in _REQUIRED_WRITES
            if minted.permissions.get(permission) != "write"
        ]
        if missing:
            raise LandingIdentityRefusedError(
                reasons.MINTED_PERMISSIONS_INSUFFICIENT,
                f"the minted token lacks {', '.join(missing)}; no request was sent",
            )
        if minted.repositories is None or [r.lower() for r in minted.repositories] != [
            name.lower()
        ]:
            raise LandingIdentityRefusedError(
                reasons.MINTED_PERMISSIONS_INSUFFICIENT,
                f"the minted token is not narrowed to exactly {owner}/{name}; "
                "no request was sent",
            )
        return self._transport_factory(SecretStr(minted.token))


__all__ = ["LANDING_MINT_PERMISSIONS", "LandingAppTransportSource"]
