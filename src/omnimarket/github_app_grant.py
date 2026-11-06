# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installation-token minting that also returns the grant (OMN-22613).

Kept beside :mod:`omnimarket.github_app_auth` so that file does not grow: the
git credential node needs the token's expiry and the permissions and
repositories GitHub says it holds, which ``mint_installation_token`` drops.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from omnimarket.github_api import rest_json
from omnimarket.github_app_auth import (
    _DEFAULT_ORG,
    _DEFAULT_PRIVATE_KEY_SECRET_NAME,
    GitHubAppIdentityError,
    _mint_app_jwt,
    _resolve_installation_id,
    assert_is_app_installation_token,
    normalize_private_key_pem,
)


@dataclass(frozen=True, repr=False)
class InstallationTokenGrant:
    """One minted installation token and what GitHub says it grants.

    ``repr`` is off so the token never reaches a log line through this object.
    """

    token: str
    expires_at: str
    permissions: dict[str, str]
    repositories: tuple[str, ...] | None


def mint_installation_token_grant(
    *,
    app_id: str,
    private_key_pem: str,
    org: str = _DEFAULT_ORG,
    repositories: Sequence[str] | None = None,
    permissions: Mapping[str, str] | None = None,
    private_key_ref: str = _DEFAULT_PRIVATE_KEY_SECRET_NAME,
    installation_id: int | None = None,
) -> InstallationTokenGrant:
    """Mint an installation token and return it with its expiry and grants.

    ``permissions``, when given, narrows the token below the App's own set
    (GitHub refuses a permission the App does not hold). The answer's
    ``repositories`` names, when GitHub returns them, are what the token was
    narrowed to; ``None`` means GitHub returned no list (an un-narrowed token).

    Raises the same errors as :func:`mint_installation_token`.
    """
    private_key_pem = normalize_private_key_pem(
        private_key_pem, declared_ref=private_key_ref
    )
    app_jwt = _mint_app_jwt(app_id, private_key_pem)
    if installation_id is None:
        installation_id = _resolve_installation_id(app_jwt, org)
    elif installation_id < 1:
        raise GitHubAppIdentityError("App installation id must be positive")
    body: dict[str, object] = {}
    if repositories:
        body["repositories"] = list(repositories)
    if permissions:
        body["permissions"] = dict(permissions)
    resp = rest_json(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        token=app_jwt,
        body=body or None,
    )
    token = resp.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubAppIdentityError(
            "App installation-token exchange returned no token"
        )
    assert_is_app_installation_token(token)
    granted = resp.get("permissions")
    listed = resp.get("repositories")
    return InstallationTokenGrant(
        token=token,
        expires_at=str(resp.get("expires_at") or ""),
        permissions=(
            {str(k): str(v) for k, v in granted.items()}
            if isinstance(granted, dict)
            else {}
        ),
        repositories=(
            tuple(
                str(entry.get("name"))
                for entry in listed
                if isinstance(entry, dict) and entry.get("name")
            )
            if isinstance(listed, list)
            else None
        ),
    )


__all__ = ["InstallationTokenGrant", "mint_installation_token_grant"]
