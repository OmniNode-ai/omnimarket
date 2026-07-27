# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub App installation-token minting for the OCC machine path (OMN-14893).

The OCC companion producers (:class:`OccCompanionEmitter`,
:class:`HandlerOccCompanionEffect`) authenticate to GitHub as a single shared
operator PAT (``GITHUB_TOKEN`` on ``.201``) today — every machine-minted
companion commit, PR, and Evidence-Source PATCH is attributed to whoever's PAT
that is, not to a machine identity. This module is the RUNTIME half of the
fix: mint a short-lived GitHub App **installation access token** on demand,
mirroring the SAME idiom already proven in CI —
``actions/create-github-app-token@v1`` as used by
``omnimarket/.github/workflows/pr-arch-review.yml`` and
``call-occ-attestation-observe.yml`` — sign a short-lived JWT with the App's
private key, resolve the App's installation, exchange for an installation
token, optionally narrowed to specific repositories.

Declared credential names (contract-level, ``secrets:`` block on both
``node_pr_lifecycle_fix_effect`` and ``node_occ_companion_effect``):
``ONEXBOT_OCC_APP_ID`` / ``ONEXBOT_OCC_PRIVATE_KEY`` — the ``onexbot-occ-writer``
App (id 148180820, installation scoped to ``onex_change_control`` today).

Fallback is made **mechanically impossible**, not merely avoided by an
``if`` branch: :func:`resolve_app_installation_token_from_contract` never
reads ``GITHUB_TOKEN`` at all — there is no code path from calling it that
can reach the PAT. A declared-but-unresolvable app credential raises
immediately, naming the missing secret ref, rather than silently falling
back to the shared PAT (the exact defect OMN-14893 exists to close;
dovetails with the OMN-14951 fail-loud ``required_secrets`` work).

The identity guard (:func:`assert_is_app_installation_token`) is the
mechanical check for ask #4: GitHub App installation tokens are always
minted with the ``ghs_`` prefix, distinct from classic PATs (``ghp_``),
OAuth tokens (``gho_``), and fine-grained PATs (``github_pat_``) — GitHub's
own documented token-prefix taxonomy. A misconfiguration that resolves a
human PAT into the app-auth code path is otherwise invisible (a companion
still gets minted, just mis-attributed — the exact class of bug this ticket
was opened to catch); the guard makes it a loud, immediate failure instead.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

import jwt as pyjwt

from omnimarket.github_api import GitHubApiError, rest_json
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref

# Installation access tokens are always issued with this prefix. Distinct from
# classic PATs (`ghp_`), OAuth app tokens (`gho_`), and fine-grained PATs
# (`github_pat_`) -- GitHub's documented token-prefix taxonomy.
_APP_INSTALLATION_TOKEN_PREFIX = "ghs_"

# GitHub's hard ceiling on App JWT lifetime is 10 minutes; 9 leaves margin for
# clock skew and the round trip to the installation-token exchange.
_JWT_TTL_SECONDS = 540
_JWT_CLOCK_SKEW_LEEWAY_SECONDS = 60

_DEFAULT_ORG = "OmniNode-ai"
_DEFAULT_APP_ID_SECRET_NAME = "ONEXBOT_OCC_APP_ID"
_DEFAULT_PRIVATE_KEY_SECRET_NAME = "ONEXBOT_OCC_PRIVATE_KEY"


class GitHubAppIdentityError(RuntimeError):
    """Raised when a resolved credential is not a genuine App installation token."""


class GitHubAppCredentialMissingError(RuntimeError):
    """Raised when a declared App credential cannot be resolved (fail-loud, no PAT fallback)."""


def assert_is_app_installation_token(token: str) -> None:
    """Guard (#4, OMN-14893): fail loud unless *token* is a genuine App token.

    Zero-network, pure prefix check — cheap enough to run on every mint. This
    is what makes a misconfigured app-auth path (one that somehow resolves a
    human PAT) a loud, immediate failure instead of an invisible
    misattribution.
    """
    if not token.startswith(_APP_INSTALLATION_TOKEN_PREFIX):
        raise GitHubAppIdentityError(
            "OCC companion machine path resolved a credential that is NOT a "
            f"GitHub App installation token (expected the {_APP_INSTALLATION_TOKEN_PREFIX!r} "
            "prefix GitHub always mints installation tokens with) -- refusing to "
            "authenticate as what may be a human PAT. This is the OMN-14893 "
            "identity guard: a misconfiguration here must never be invisible."
        )


def _mint_app_jwt(app_id: str, private_key_pem: str) -> str:
    """Sign a short-lived App JWT (RS256), the same shape GitHub's own
    ``actions/create-github-app-token`` action produces."""
    now = int(time.time())
    payload = {
        "iat": now - _JWT_CLOCK_SKEW_LEEWAY_SECONDS,
        "exp": now + _JWT_TTL_SECONDS,
        "iss": app_id,
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="RS256")


def _resolve_installation_id(app_jwt: str, org: str) -> int:
    info = rest_json("GET", f"/orgs/{org}/installation", token=app_jwt)
    installation_id = info.get("id")
    if not isinstance(installation_id, int):
        raise GitHubAppIdentityError(
            f"could not resolve the App installation id for org {org!r}: {info!r}"
        )
    return installation_id


def mint_installation_token(
    *,
    app_id: str,
    private_key_pem: str,
    org: str = _DEFAULT_ORG,
    repositories: Sequence[str] | None = None,
) -> str:
    """Mint a short-lived GitHub App installation access token.

    Mirrors the ``actions/create-github-app-token@v1`` idiom already proven in
    CI: sign a JWT, resolve the App's installation on ``org``, exchange for an
    installation access token — narrowed to ``repositories`` when given (the
    same narrowing ``call-occ-attestation-observe.yml`` applies with
    ``repositories: onex_change_control``).

    Raises:
        GitHubApiError: on any GitHub API transport failure.
        GitHubAppIdentityError: if the exchange response carries no token, or
            the minted token fails :func:`assert_is_app_installation_token`.
    """
    app_jwt = _mint_app_jwt(app_id, private_key_pem)
    installation_id = _resolve_installation_id(app_jwt, org)
    body: dict[str, object] = {}
    if repositories:
        body["repositories"] = list(repositories)
    resp = rest_json(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        token=app_jwt,
        body=body or None,
    )
    token = resp.get("token")
    if not isinstance(token, str) or not token:
        raise GitHubAppIdentityError(
            f"App installation-token exchange returned no token: {resp!r}"
        )
    assert_is_app_installation_token(token)
    return token


def resolve_app_installation_token_from_contract(
    contract_path: Path,
    *,
    org: str = _DEFAULT_ORG,
    repositories: Sequence[str] | None = None,
    app_id_secret_name: str = _DEFAULT_APP_ID_SECRET_NAME,
    private_key_secret_name: str = _DEFAULT_PRIVATE_KEY_SECRET_NAME,
) -> str:
    """Resolve + mint an App installation token from contract-declared secrets.

    This is the ONLY entry point the OCC companion producers call in
    app-auth mode. It never reads ``GITHUB_TOKEN`` — there is no branch, flag,
    or exception path inside this function that can reach the operator PAT,
    so a caller that reaches this function cannot silently fall back to it.
    A declared-but-unresolvable credential raises
    :class:`GitHubAppCredentialMissingError` naming the exact missing secret
    ref, rather than degrading.

    Args:
        contract_path: The calling node's ``contract.yaml`` (must declare
            both ``app_id_secret_name`` and ``private_key_secret_name`` under
            its ``secrets:`` block — see ``contract_secret_ref``).
        org: GitHub org the App is installed on.
        repositories: Optional repo-name narrowing for the minted token (no
            owner prefix, e.g. ``["onex_change_control"]``).
    """
    app_id_ref = contract_secret_ref(contract_path, app_id_secret_name)
    private_key_ref = contract_secret_ref(contract_path, private_key_secret_name)

    app_id_secret = resolve_api_key(
        app_id_ref, env_var_fallback=app_id_ref, required=False
    )
    if app_id_secret is None:
        raise GitHubAppCredentialMissingError(
            f"OCC app-auth mode requires {app_id_ref!r} (declared in "
            f"{contract_path} secrets:), but it did not resolve from the "
            "secret store or environment. No PAT fallback exists in "
            "app-auth mode (OMN-14893) -- provision the credential or fall "
            "back to pat mode explicitly (OMNI_OCC_GITHUB_AUTH_MODE=pat)."
        )
    private_key_secret = resolve_api_key(
        private_key_ref, env_var_fallback=private_key_ref, required=False
    )
    if private_key_secret is None:
        raise GitHubAppCredentialMissingError(
            f"OCC app-auth mode requires {private_key_ref!r} (declared in "
            f"{contract_path} secrets:), but it did not resolve from the "
            "secret store or environment. No PAT fallback exists in "
            "app-auth mode (OMN-14893) -- provision the credential or fall "
            "back to pat mode explicitly (OMNI_OCC_GITHUB_AUTH_MODE=pat)."
        )

    try:
        return mint_installation_token(
            app_id=app_id_secret.get_secret_value(),
            private_key_pem=private_key_secret.get_secret_value(),
            org=org,
            repositories=repositories,
        )
    except GitHubApiError as exc:
        raise GitHubAppIdentityError(
            f"App installation-token mint failed for org {org!r}: {exc}"
        ) from exc


__all__ = [
    "GitHubAppCredentialMissingError",
    "GitHubAppIdentityError",
    "assert_is_app_installation_token",
    "mint_installation_token",
    "resolve_app_installation_token_from_contract",
]
