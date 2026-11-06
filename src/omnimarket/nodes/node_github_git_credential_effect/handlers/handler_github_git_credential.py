# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""EFFECT: issue one repository's git credential, sealed to its requester (OMN-22613).

Checks come before any GitHub call, in this order: the repository names an
OWNER/NAME, the owner is allowed and the repository is not denied by the
overlay, and the requester's key is a usable X25519 public key. Only then is the
writer App asked for a token narrowed to that one repository and the overlay's
permission set. A token GitHub returns wider than asked (another repository, or
a permission above the asked level) is refused, not issued. The token is sealed
to the requester's key before it leaves this handler; it is never logged.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from importlib import resources
from typing import Final

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.github_credential_seal import (
    SealError,
    parse_public_key,
    seal_token,
)
from omnimarket.nodes.node_github_git_credential_effect.models import (
    EnumGitCredentialRefusal,
    ModelGitCredentialFailed,
    ModelGitCredentialIssued,
    ModelGitCredentialPolicy,
    ModelGitCredentialRequest,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols import (
    GitCredentialMintRefusedError,
    GitCredentialUnavailableError,
    MintedRepositoryToken,
    ProtocolRepositoryTokenMinter,
)
from omnimarket.nodes.node_github_git_credential_effect.protocols.app_repository_token_minter import (
    AppRepositoryTokenMinter,
)

NODE_NAME: Final[str] = "node_github_git_credential_effect"
_REPOSITORY: Final[re.Pattern[str]] = re.compile(
    r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/(?P<name>[A-Za-z0-9_.-]{1,100})$"
)
_LEVEL: Final[dict[str, int]] = {"read": 1, "write": 2}
_log = logging.getLogger(__name__)

Result = ModelGitCredentialIssued | ModelGitCredentialFailed


def packaged_policy() -> ModelGitCredentialPolicy:
    """The node's packaged overlay, the default when no deployment overlay is set."""
    text = (
        resources.files(f"omnimarket.nodes.{NODE_NAME}")
        .joinpath("overlay.yaml")
        .read_text(encoding="utf-8")
    )
    return ModelGitCredentialPolicy.model_validate(yaml.safe_load(text))


def resolve_policy() -> ModelGitCredentialPolicy:
    """A deployment overlay under ONEX_SKILL_OVERLAY_ROOTS wins; else the packaged one."""
    try:
        return load_structured_overlay(NODE_NAME, ModelGitCredentialPolicy)
    except NodeOverlayNotFoundError:
        return packaged_policy()


def split_repository(text: str) -> tuple[str, str] | None:
    """``(owner, name)`` of OWNER/NAME (a trailing .git dropped), or None."""
    candidate = text.strip()
    if candidate.endswith(".git"):
        candidate = candidate[: -len(".git")]
    match = _REPOSITORY.match(candidate)
    if match is None or match["name"] in {".", ".."}:
        return None
    return match["owner"], match["name"]


def _wider_than_asked(
    minted: MintedRepositoryToken, name: str, asked: dict[str, str]
) -> str | None:
    if minted.repositories is None or [r.lower() for r in minted.repositories] != [
        name.lower()
    ]:
        return "the token is not narrowed to exactly the one repository asked for"
    for permission, level in minted.permissions.items():
        if permission not in asked or _LEVEL.get(level, 3) > _LEVEL[asked[permission]]:
            return f"the token carries {permission}={level}, above what was asked"
    return None


def _expiry(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise GitCredentialMintRefusedError(
            "the mint answered with no readable expiry"
        ) from None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class HandlerGithubGitCredentialEffect:
    """Issue one sealed, repository-narrowed push credential, or a typed refusal.

    ``minter`` and ``policy`` are injected in tests. Without them the handler
    reads its overlay and mints through the writer App's store-resolved refs.
    """

    def __init__(
        self,
        minter: ProtocolRepositoryTokenMinter | None = None,
        *,
        policy: ModelGitCredentialPolicy | None = None,
    ) -> None:
        self._policy = policy
        self._minter = minter

    def _resolved(
        self,
    ) -> tuple[ModelGitCredentialPolicy, ProtocolRepositoryTokenMinter]:
        if self._policy is None:
            self._policy = resolve_policy()
        if self._minter is None:
            self._minter = AppRepositoryTokenMinter(self._policy)
        return self._policy, self._minter

    async def handle(self, request: ModelGitCredentialRequest) -> Result:
        policy, minter = self._resolved()

        def failed(reason: EnumGitCredentialRefusal, detail: str) -> Result:
            _log.info(
                "git credential refused: repository=%s reason=%s lane=%s host=%s",
                request.repository,
                reason.value,
                request.requested_by_lane,
                request.requested_by_host,
            )
            return ModelGitCredentialFailed(
                correlation_id=request.correlation_id,
                repository=request.repository,
                reason=reason,
                detail=detail,
                requested_by_lane=request.requested_by_lane,
                requested_by_host=request.requested_by_host,
            )

        parts = split_repository(request.repository)
        if parts is None:
            return failed(
                EnumGitCredentialRefusal.REPOSITORY_MALFORMED,
                "the repository must be OWNER/NAME",
            )
        owner, name = parts
        if owner.lower() not in {o.lower() for o in policy.allowed_owners}:
            return failed(
                EnumGitCredentialRefusal.OWNER_NOT_ALLOWED,
                f"owner {owner!r} is not in this node's allowed_owners overlay setting",
            )
        full = f"{owner}/{name}".lower()
        if full in {r.lower() for r in policy.denied_repositories}:
            return failed(
                EnumGitCredentialRefusal.REPOSITORY_NOT_ALLOWED,
                f"{owner}/{name} is in this node's denied_repositories overlay setting",
            )
        try:
            recipient = parse_public_key(request.recipient_public_key)
        except (SealError, ValueError) as exc:
            return failed(
                EnumGitCredentialRefusal.RECIPIENT_KEY_MALFORMED,
                f"the recipient key is not a base64url X25519 public key: {exc}",
            )
        asked: dict[str, str] = dict(policy.permissions)
        try:
            minted = await minter.mint(owner=owner, name=name, permissions=asked)
            wider = _wider_than_asked(minted, name, asked)
            if wider is not None:
                raise GitCredentialMintRefusedError(wider)
            expires_at = _expiry(minted.expires_at)
        except GitCredentialUnavailableError as exc:
            return failed(EnumGitCredentialRefusal.CREDENTIAL_UNAVAILABLE, str(exc))
        except GitCredentialMintRefusedError as exc:
            return failed(EnumGitCredentialRefusal.MINT_REFUSED, str(exc))
        sealed = seal_token(
            minted.token,
            recipient,
            correlation_id=request.correlation_id,
            repository=request.repository,
        )
        _log.info(
            "git credential issued: repository=%s/%s expires_at=%s lane=%s host=%s",
            owner,
            name,
            expires_at.isoformat(),
            request.requested_by_lane,
            request.requested_by_host,
        )
        return ModelGitCredentialIssued(
            correlation_id=request.correlation_id,
            repository=request.repository,
            sealed_token=sealed,
            expires_at=expires_at,
            permissions=dict(minted.permissions),
            requested_by_lane=request.requested_by_lane,
            requested_by_host=request.requested_by_host,
        )


__all__ = [
    "NODE_NAME",
    "HandlerGithubGitCredentialEffect",
    "packaged_policy",
    "resolve_policy",
    "split_repository",
]
