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

import logging
import re
import time
from collections.abc import Sequence
from pathlib import Path

import jwt as pyjwt
from cryptography.hazmat.primitives.serialization import load_pem_private_key

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

logger = logging.getLogger(__name__)

# A PEM private key is `-----BEGIN <label>-----`, a base64 body wrapped at 64
# columns, then `-----END <label>-----`. Env-var transport is newline-hostile:
# a docker `.env` / compose `environment:` round trip routinely arrives with the
# body newlines collapsed to spaces or removed outright, and sometimes with the
# original shell quoting still attached. The armor survives; only the framing is
# lost, so the mangling is deterministic and losslessly reversible -- which is
# why this module re-frames rather than refuses, then PROVES the result by
# loading it (OMN-18069).
_PEM_ARMOR_RE = re.compile(
    r"-----BEGIN (?P<label>[A-Z0-9 ]*PRIVATE KEY)-----"
    r"(?P<body>.*?)"
    r"-----END (?P=label)-----",
    re.DOTALL,
)
_PEM_BODY_ALLOWED_RE = re.compile(r"^[A-Za-z0-9+/=]*$")
_PEM_WRAP_COLUMNS = 64


class GitHubAppIdentityError(RuntimeError):
    """Raised when a resolved credential is not a genuine App installation token."""


class GitHubAppCredentialMissingError(RuntimeError):
    """Raised when a declared App credential cannot be resolved (fail-loud, no PAT fallback)."""


class GitHubAppCredentialMalformedError(RuntimeError):
    """Raised when a declared App credential resolves but is not a usable key.

    OMN-18069. The prior guard was ``is None`` only, so a credential that was
    *present but unparseable* skipped the fail-loud branch entirely and surfaced
    three frames down as ``jwt.exceptions.InvalidKeyError: Could not parse the
    provided public key.`` -- a message that names the wrong key kind, names no
    secret ref, and reads like a code defect rather than the config-delivery one
    it is. On 2026-09-09 that single misdirection cost 37 consecutive silent OCC
    autobind failures across 17 product PRs in 6 repos before anyone read a
    container log.

    The message carries a SHAPE description (length, armor present, newline
    count) and never the value.
    """


def _pem_shape(*, length: int, armored: bool, newline_count: int) -> str:
    """Render a credential's SHAPE for an error message. NEVER the value.

    Takes primitives -- an int, a bool and an int -- rather than the credential
    itself, so the secret string has no data path into any message or log
    expression at all. That is a structural guarantee, not a convention: there
    is nothing here to accidentally interpolate.

    Length, armor presence and surviving newline count are exactly enough to
    tell a newline-stripped PEM (the OMN-18069 defect) apart from an empty
    value, a truncated one, or something that was never a key.
    """
    return f"length={length} pem_armor_present={armored} newline_count={newline_count}"


def normalize_private_key_pem(value: str, *, secret_ref: str) -> str:
    """Return *value* as a PEM that :mod:`cryptography` can actually load.

    OMN-18069. Env-var transport strips the body newlines a PEM needs; the
    armor lines survive, so the damage is deterministic and reversible. This
    re-frames the base64 body at 64 columns and then PROVES the result by
    loading it -- a repair that cannot be wrong, because a re-framing that
    produced a different key would not load.

    It is deliberately not silent: a value that needed re-framing logs a
    WARNING naming *secret_ref* -- and only *secret_ref*, nothing derived from
    the value, not even its length -- so the upstream config-delivery defect
    stays visible instead of being absorbed here. A value that still will not
    load raises :class:`GitHubAppCredentialMalformedError` naming *secret_ref*
    and the SHAPE, never the value.

    Args:
        value: The resolved credential, as delivered.
        secret_ref: The declared secret ref, for the log line and the error.

    Raises:
        GitHubAppCredentialMalformedError: if no PEM private-key armor is
            present, if the body is not base64, or if the re-framed PEM still
            does not load as a private key.
    """
    shape = _pem_shape(
        length=len(value),
        armored=bool(_PEM_ARMOR_RE.search(value)),
        newline_count=value.count("\n"),
    )
    candidate = value.strip().strip('"').strip("'").strip()
    match = _PEM_ARMOR_RE.search(candidate)
    if match is None:
        raise GitHubAppCredentialMalformedError(
            f"OCC app-auth mode resolved {secret_ref!r} but it carries no PEM "
            f"private-key armor ({shape}). This is a config-delivery "
            "defect at the seam that populates the container environment, not a "
            "credential that needs rotating -- repair the transport. The value "
            "is never logged."
        )

    label = match.group("label")
    body = "".join(match.group("body").split())
    if not _PEM_BODY_ALLOWED_RE.match(body) or not body:
        raise GitHubAppCredentialMalformedError(
            f"OCC app-auth mode resolved {secret_ref!r} with PEM armor but a body "
            f"that is not base64 ({shape}). Repair the transport that "
            "populates the container environment. The value is never logged."
        )

    wrapped = "\n".join(
        body[i : i + _PEM_WRAP_COLUMNS] for i in range(0, len(body), _PEM_WRAP_COLUMNS)
    )
    reframed = f"-----BEGIN {label}-----\n{wrapped}\n-----END {label}-----\n"

    try:
        load_pem_private_key(reframed.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise GitHubAppCredentialMalformedError(
            f"OCC app-auth mode resolved {secret_ref!r} but it does not load as a "
            f"private key even after PEM re-framing ({shape}): {exc}. "
            "Repair the transport that populates the container environment; do "
            "not rotate on this signal alone. The value is never logged."
        ) from exc

    if reframed != candidate and reframed.rstrip("\n") != candidate.rstrip("\n"):
        # The log line names the secret REF and nothing derived from the secret
        # VALUE -- not even its length. The shape string stays on the raise
        # paths, where it is needed to tell a mangled key from an absent one and
        # where no logging sink is involved. Deliberate: a static analyser
        # cannot distinguish `len(secret)` from `secret`, and arguing with it by
        # suppression would leave the next reader unable to either. Withholding
        # a byte count from one WARNING costs nothing; the condition itself is
        # the diagnosis.
        logger.warning(
            "github_app_auth: %s arrived in a form PEM parsers reject and was "
            "re-framed in-process to load. This is a CONFIG-DELIVERY defect "
            "upstream of the runtime -- repair the seam that populates the "
            "container environment. OMN-18069.",
            secret_ref,
        )
    return reframed


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
    private_key_ref: str = _DEFAULT_PRIVATE_KEY_SECRET_NAME,
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
        GitHubAppCredentialMalformedError: if ``private_key_pem`` is present
            but is not a loadable private key (OMN-18069) -- raised HERE,
            naming ``private_key_ref``, instead of surfacing three frames down
            as pyjwt's ``InvalidKeyError: Could not parse the provided public
            key.``
    """
    private_key_pem = normalize_private_key_pem(
        private_key_pem, secret_ref=private_key_ref
    )
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
            private_key_ref=private_key_ref,
        )
    except GitHubApiError as exc:
        raise GitHubAppIdentityError(
            f"App installation-token mint failed for org {org!r}: {exc}"
        ) from exc


__all__ = [
    "GitHubAppCredentialMalformedError",
    "GitHubAppCredentialMissingError",
    "GitHubAppIdentityError",
    "assert_is_app_installation_token",
    "mint_installation_token",
    "normalize_private_key_pem",
    "resolve_app_installation_token_from_contract",
]
