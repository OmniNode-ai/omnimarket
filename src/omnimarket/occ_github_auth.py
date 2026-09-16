# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ONE seam every OCC node resolves its GitHub credential through (OMN-18439).

OMN-14893 gave the OCC companion producers an auth-mode switch: ``pat`` reads
the contract-declared ``GITHUB_TOKEN``, ``app`` mints a short-lived
``onexbot-occ-writer`` installation token through
:func:`~omnimarket.github_app_auth.resolve_app_installation_token_from_contract`,
which has no code path back to the PAT. That switch was then written out three
times -- once in ``occ_companion_emitter``, once in
``handler_occ_companion_effect``, and NOT AT ALL in
``handler_occ_state_effect``, which resolved ``GITHUB_TOKEN`` unconditionally.

The omission was invisible because the two halves of a mint sit in different
modules but the same process. ``HandlerOccCompanionEffect._mint_once`` calls
``HandlerOccStateEffect.handle`` as the FIRST GitHub I/O of every mint, so a
lane running ``OMNI_OCC_GITHUB_AUTH_MODE=app`` -- which the ``.201`` effects
lane does -- still spent roughly eight REST calls of the shared operator PAT's
budget on the read half before the App credential was touched. Measured on that
lane on 2026-09-16: 16 of the newest 30 records on
``onex.evt.omnimarket.occ-companion-effect-failed.v1`` carried GitHub's own
``API rate limit exceeded for user ID 1002253``, and ``gh api user`` resolves
1002253 to the shared operator account.

Duplicating a credential decision is how the two halves diverged, so this
module holds the only definition. A node calls :func:`resolve_occ_github_token`
with its OWN ``contract.yaml`` -- the secret refs are read out of the contract
it is handed, so passing a sibling's contract would make the caller's own
``secrets:`` block decorative.

Why the mode is still read from the environment rather than declared in each
contract: the CI mutate path (``call-occ-companion-author.yml``) deliberately
runs these nodes in ``pat`` mode while exporting a NARROWED,
``onex_change_control``-scoped App installation token as ``GITHUB_TOKEN``
(OMN-15350 / OMN-15441). A contract that declared ``app`` unconditionally would
make that job mint an org-wide token instead -- a privilege BROADENING, and the
opposite of what those tickets established. The mode is a per-deployment
property of which credential is present, not a property of the node, so it
stays where the deployment sets it; the credential REFS, which are properties
of the node, stay contract-declared. Narrowing the mint's repository scope so
the runtime can declare ``app`` without widening CI is the follow-up, not this
change.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from omnimarket.github_app_auth import resolve_app_installation_token_from_contract
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref

__all__ = [
    "GITHUB_AUTH_MODE_ENV_VAR",
    "EnumOccGitHubAuthMode",
    "resolve_occ_auth_mode",
    "resolve_occ_github_token",
]

#: Selects which credential the OCC nodes authenticate with. Bootstrap-only
#: (which credential the deployment holds), never an endpoint or a policy.
GITHUB_AUTH_MODE_ENV_VAR = "OMNI_OCC_GITHUB_AUTH_MODE"

#: The contract-declared ref name every OCC node uses for its PAT-mode token.
GITHUB_TOKEN_SECRET_NAME = "GITHUB_TOKEN"


class EnumOccGitHubAuthMode(StrEnum):
    """How an OCC node authenticates to GitHub.

    ``PAT`` is the default because it is the mode the CI mutate path runs in,
    and because a runtime that has not been given the App credentials must keep
    working rather than hard-fail on a mode it cannot satisfy.
    """

    PAT = "pat"
    APP = "app"


def resolve_occ_auth_mode() -> EnumOccGitHubAuthMode:
    """Resolve the deployment's OCC auth mode, failing closed on anything else.

    An unrecognised value raises rather than silently degrading to ``pat``. A
    typo that quietly fell back to the PAT would reintroduce exactly the
    mis-attribution OMN-14893 closed, and it would do so invisibly -- the mint
    would still succeed, just on the wrong identity.
    """
    raw = os.environ.get(GITHUB_AUTH_MODE_ENV_VAR, "").strip().lower()
    if not raw:
        return EnumOccGitHubAuthMode.PAT
    try:
        return EnumOccGitHubAuthMode(raw)
    except ValueError:
        recognized = ", ".join(repr(mode.value) for mode in EnumOccGitHubAuthMode)
        raise RuntimeError(
            f"{GITHUB_AUTH_MODE_ENV_VAR}={raw!r} is not a recognized OCC "
            f"GitHub auth mode (expected one of {recognized})."
        ) from None


def resolve_occ_github_token(contract_path: Path) -> str:
    """Resolve the GitHub credential the calling OCC node authenticates with.

    In ``app`` mode this delegates to
    :func:`~omnimarket.github_app_auth.resolve_app_installation_token_from_contract`,
    which reads only the App credential refs and has no branch, flag or
    exception path that can reach ``GITHUB_TOKEN``. The absence of a PAT
    fallback there is structural, not a convention this function upholds -- so
    a caller in app mode cannot fall back to the operator PAT even if this
    function were wrong.

    In ``pat`` mode it resolves the contract-declared ``GITHUB_TOKEN`` ref
    through the secret store with that ref name as the literal env-var fallback
    (OMN-14452): the deployed effects lane's resolver is configured with an
    LLM/Slack-only mapping and no convention fallback, so it never resolves
    ``GITHUB_TOKEN`` itself, while the container does pass it through as a
    literal env var.

    Args:
        contract_path: The CALLING node's ``contract.yaml``. Both the App
            credential refs and the ``GITHUB_TOKEN`` ref are read out of it, so
            a node must pass its own and not a sibling's.

    Returns:
        The token, either a freshly minted installation token or the
        contract-declared PAT-mode credential.

    Raises:
        RuntimeError: The auth mode is unrecognised, or pat mode's declared ref
            resolved to nothing.
        GitHubAppCredentialMissingError: App mode is selected and a declared App
            credential did not resolve. Deliberately not caught: degrading to
            the PAT here is the defect.
    """
    if resolve_occ_auth_mode() is EnumOccGitHubAuthMode.APP:
        return resolve_app_installation_token_from_contract(contract_path)

    ref = contract_secret_ref(contract_path, GITHUB_TOKEN_SECRET_NAME)
    secret = resolve_api_key(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            f"ensure {GITHUB_TOKEN_SECRET_NAME} is set in the secret store."
        )
    return secret.get_secret_value()
