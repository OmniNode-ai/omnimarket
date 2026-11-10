# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AppTokenMinter: mints a fresh installation token from the refs the overlay names.

The three refs resolve the way the credential minter does (``resolve_api_key`` with
``env_var_fallback=ref, required=False``). Errors name the ref and the exception type only,
never a value or a response body.
"""

from __future__ import annotations

from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import (
    GitHubAppCredentialMalformedError,
    GitHubAppIdentityError,
    mint_installation_token,
)
from omnimarket.inference.secret_store_resolver import (
    SecretStoreConfigurationError,
    resolve_api_key,
)
from omnimarket.nodes.node_runner_group_exposure_effect.overlay import (
    ModelGithubAppMint,
)
from omnimarket.nodes.node_runner_group_exposure_effect.protocols.protocol_token_minter import (
    ExposureTokenUnavailableError,
)


def _resolve(ref: str) -> str:
    """Resolve one ref to a value, naming the ref and the error type only on failure."""
    try:
        value = resolve_api_key(ref, env_var_fallback=ref, required=False)
    except SecretStoreConfigurationError:
        raise ExposureTokenUnavailableError(
            f"the secret store for ref {ref!r} cannot be built here (SecretStoreConfigurationError)"
        ) from None
    except (
        Exception
    ) as exc:  # a store or SDK error may carry a secret: drop its text and cause
        raise ExposureTokenUnavailableError(
            f"secret lookup failed for ref {ref!r} ({type(exc).__name__})"
        ) from None
    if value is None:
        raise ExposureTokenUnavailableError(f"ref {ref!r} does not resolve here")
    return value.get_secret_value()


class AppTokenMinter:
    """Mints one installation token through the store-resolved refs the overlay names."""

    def mint(self, *, org: str, mint: ModelGithubAppMint) -> str:
        app_id = _resolve(mint.app_id_ref)
        private_key_pem = _resolve(mint.private_key_ref)
        installation_raw = _resolve(mint.installation_id_ref)
        try:
            installation_id = int(installation_raw)
        except ValueError:
            raise ExposureTokenUnavailableError(
                f"ref {mint.installation_id_ref!r} is not an integer installation id (ValueError)"
            ) from None
        try:
            return mint_installation_token(
                app_id=app_id,
                private_key_pem=private_key_pem,
                org=org,
                installation_id=installation_id,
                private_key_ref=mint.private_key_ref,
            )
        except (
            GitHubApiError,
            GitHubAppIdentityError,
            GitHubAppCredentialMalformedError,
            SecretStoreConfigurationError,
        ) as exc:
            raise ExposureTokenUnavailableError(
                f"the App installation-token mint was refused ({type(exc).__name__})"
            ) from None
        except (
            Exception
        ) as exc:  # anything else: name the type only, never a value or body
            raise ExposureTokenUnavailableError(
                f"the App installation-token mint failed ({type(exc).__name__})"
            ) from None


__all__ = ["AppTokenMinter"]
