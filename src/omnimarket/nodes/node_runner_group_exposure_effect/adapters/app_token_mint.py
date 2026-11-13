# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The App installation-token minter: one fresh token per mint, from named secrets.

The overlay names the App id and the App private key by their secret-store references; this
adapter resolves the two values at mint time and never logs a value or puts one in an error.
"""

from __future__ import annotations

from omnimarket.github_api import GitHubApiError
from omnimarket.github_app_auth import (
    GitHubAppCredentialMissingError,
    GitHubAppIdentityError,
    mint_installation_token,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key


class AppInstallationTokenMint:
    def mint(self, *, org: str, app_id_secret: str, private_key_secret: str) -> str:
        app_id = resolve_api_key(
            app_id_secret, env_var_fallback=app_id_secret, required=False
        )
        if app_id is None:
            raise GitHubAppCredentialMissingError(
                f"token mint requires {app_id_secret!r}, but it did not resolve "
                "from the secret store or environment"
            )
        private_key = resolve_api_key(
            private_key_secret,
            env_var_fallback=private_key_secret,
            required=False,
        )
        if private_key is None:
            raise GitHubAppCredentialMissingError(
                f"token mint requires {private_key_secret!r}, but it did not "
                "resolve from the secret store or environment"
            )
        try:
            return mint_installation_token(
                app_id=app_id.get_secret_value(),
                private_key_pem=private_key.get_secret_value(),
                org=org,
                private_key_ref=private_key_secret,
            )
        except GitHubApiError as error:
            raise GitHubAppIdentityError(
                f"App installation-token mint failed for org {org!r}: {error}"
            ) from error
