# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The live minter: the writer App's installation token for one repository (OMN-22613).

The App id and signing key come from the secret store through the refs the
contract declares and the overlay names. Nothing here reads a person's token:
there is no code path from this class to ``GITHUB_TOKEN``. Errors carry the ref
names and an HTTP status, never a value or a response body.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path

from pydantic import SecretStr

from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import mint_installation_token_grant
from omnimarket.inference.secret_store_resolver import (
    SecretStoreConfigurationError,
    resolve_api_key_async,
)
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_git_credential_effect.models import (
    ModelGitCredentialPolicy,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    GitCredentialMintRefusedError,
    GitCredentialUnavailableError,
    MintedRepositoryToken,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


async def _from_store(ref: str) -> SecretStr | None:
    try:
        return await resolve_api_key_async(ref, env_var_fallback=ref, required=False)
    except SecretStoreConfigurationError as exc:
        # Names variables and logical names only, never a value (OMN-16984).
        raise GitCredentialUnavailableError(
            f"the secret store for ref {ref!r} cannot be built here: {exc}"
        ) from None
    except Exception:
        # A store or SDK error may carry a secret: drop its text and cause.
        raise GitCredentialUnavailableError(
            f"secret lookup failed for ref {ref!r}"
        ) from None


class HandlerAppRepositoryTokenMinter:
    """Mints the writer App's token through the store-resolved refs the policy names."""

    def __init__(self, policy: ModelGitCredentialPolicy) -> None:
        self._policy = policy

    async def mint(
        self, *, owner: str, name: str, permissions: Mapping[str, str]
    ) -> MintedRepositoryToken:
        policy = self._policy
        refs = [
            contract_secret_ref(_CONTRACT_PATH, secret)
            for secret in (
                policy.app_id_secret,
                policy.private_key_secret,
                policy.installation_id_secret,
            )
        ]
        app_id, private_key, installation = [await _from_store(ref) for ref in refs]
        if app_id is None or private_key is None:
            raise GitCredentialUnavailableError(
                f"App refs {refs[0]!r} and {refs[1]!r} must both resolve here"
            )
        installation_id: int | None = None
        if installation is not None:
            try:
                installation_id = int(installation.get_secret_value())
            except ValueError:
                raise GitCredentialUnavailableError(
                    f"ref {refs[2]!r} is not an integer installation id"
                ) from None
        try:
            # The minter is synchronous; keep it off the runtime's event loop.
            grant = await asyncio.to_thread(
                mint_installation_token_grant,
                app_id=app_id.get_secret_value(),
                private_key_pem=private_key.get_secret_value(),
                org=owner,
                repositories=[name],
                permissions=dict(permissions),
                private_key_ref=refs[1],
                installation_id=installation_id,
            )
        except Exception as exc:
            # SDK and HTTP errors may hold response bodies: keep the status only.
            status = exc.status_code if isinstance(exc, GitHubApiError) else None
            raise GitCredentialMintRefusedError(
                f"the App installation-token mint for {owner}/{name} was refused"
                + (f" (HTTP {status})" if status is not None else ""),
                http_status=status,
            ) from None
        return MintedRepositoryToken(
            token=grant.token,
            expires_at=grant.expires_at,
            permissions=grant.permissions,
            repositories=grant.repositories,
        )


__all__ = ["HandlerAppRepositoryTokenMinter"]
