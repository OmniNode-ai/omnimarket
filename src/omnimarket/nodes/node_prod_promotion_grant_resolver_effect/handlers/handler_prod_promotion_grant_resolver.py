# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_prod_promotion_grant_resolver_effect (OMN-13439 / Phase 2b).

EFFECT node. The orchestrator fact-gathering boundary that resolves the prod
promotion grant from the durable trust anchor BEFORE the prod gate evaluates.

Anti-self-approval (OMN-10971): the grant is fetched from
``omninode_infra@main`` — NOT a PR branch — exactly as
``reject-deploy-gate-skip.yml`` fetches its skip-token allowlist
(``contents/<path>?ref=main``). A redeploy request therefore cannot author the
authorization that approves it, even by editing the grant file in the same change.

The handler:
  1. resolves the anchor read token from the contract secret ref
     ``ONEX_GRANT_ANCHOR_READ_TOKEN`` through the secret store at the effect
     boundary (no bare ``os.environ`` read, no subprocess shell-out). The ref
     is dedicated so a token scoped for public repositories is never reused
     for the private anchor (OMN-20068);
  2. fetches the grant file bytes + source commit SHA from ``main`` and probes
     whether the file is CODEOWNERS-protected on that ref;
  3. parses the YAML directly (ZERO Python import on the repository that holds it) and
     resolves it against the request key via the pure ``grant_resolver``;
  4. returns a resolve failure as a typed UNREADABLE / UNPARSEABLE refusal with
     an :class:`EnumGrantAnchorRefusal` kind, never raised, or the resolved
     grant, plus durable audit provenance. A token that cannot read the private
     anchor (404 on the ``main`` commit read) is ``repository_unreadable``; a
     grant file absent at a commit the token did read is ``grant_file_missing``. The
     runtime wraps the returned event for dispatch/emission — this handler
     is a thin typed transform, not an envelope producer.

Provenance lives on the EMITTED AUDIT EVIDENCE, never on the pure grant DTO.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import urllib.error
import urllib.request
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import (
    GRANT_FETCH_REF,
    GRANT_FILE_PATH,
    GRANT_REPO,
    EnumGrantAnchorRefusal,
    EnumGrantResolution,
    ModelGrantProvenance,
    ModelProdPromotionGrantResolveCommand,
    ModelProdPromotionGrantResolvedEvent,
)
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_prod_promotion_grant_resolver_effect.grant_resolver import (
    file_sha256,
    resolve_grant,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
#: Logical name of the anchor read token under the contract ``secrets`` block.
_ANCHOR_TOKEN_SECRET = "ONEX_GRANT_ANCHOR_READ_TOKEN"
logger = logging.getLogger(__name__)

# GitHub API host for the grant-anchor read. This is the public api.github.com
# control-plane host (no per-model routing authority applies to a VCS read of the
# governance anchor), matching node_github_review_effect's identical I/O boundary.
_GITHUB_API_BASE = "https://api.github.com"  # url-authority-ok: GitHub control-plane host for the omninode_infra@main grant read; no model routing authority
_GITHUB_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT = 30.0
_CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


class EnumGrantAnchorReadStage(StrEnum):
    """Which anchor read failed. The stage is what tells a 404 apart."""

    MAIN_COMMIT = "main_commit"
    GRANT_FILE = "grant_file"
    CODEOWNERS = "codeowners"


class GrantAnchorReadError(Exception):
    """An HTTP failure on one anchor read, tagged with the read that failed."""

    def __init__(self, stage: EnumGrantAnchorReadStage, status: int) -> None:
        super().__init__(f"anchor read {stage.value} failed with HTTP {status}")
        self.stage = stage
        self.status = status


def _http_refusal(
    status: int, stage: EnumGrantAnchorReadStage | None
) -> tuple[EnumGrantAnchorRefusal, str]:
    """Classify an anchor HTTP failure without including credentials.

    ``stage`` is ``None`` only for an injected fetcher that raised a bare
    ``HTTPError``; the deployed fetcher always tags the stage.
    """
    if status == 401:
        return (
            EnumGrantAnchorRefusal.TOKEN_REJECTED,
            "token rejected: GitHub returned 401 (bad, expired or revoked token)",
        )
    if status == 403:
        return (
            EnumGrantAnchorRefusal.TOKEN_FORBIDDEN,
            "token forbidden: GitHub returned 403 (token lacks a permission such as "
            "Contents read, SSO not authorized, or rate limited)",
        )
    if status == 404 and stage is EnumGrantAnchorReadStage.MAIN_COMMIT:
        return (
            EnumGrantAnchorRefusal.REPOSITORY_UNREADABLE,
            f"token cannot read {GRANT_REPO}: GitHub returned 404 on the "
            f"{GRANT_FETCH_REF} commit read, which needs only Contents read; for a "
            "private repository that is a token without access to it (or no such "
            "repository), never a missing grant file",
        )
    if status == 404 and stage is EnumGrantAnchorReadStage.GRANT_FILE:
        return (
            EnumGrantAnchorRefusal.GRANT_FILE_MISSING,
            f"grant file {GRANT_FILE_PATH} is absent at the {GRANT_FETCH_REF} commit "
            "the token did read: the token can read the anchor, the file is not there",
        )
    where = stage.value if stage is not None else "unknown stage"
    return (
        EnumGrantAnchorRefusal.HTTP_ERROR,
        f"anchor read failed: GitHub returned HTTP {status} ({where})",
    )


def _decode_content(content: object) -> bytes:
    """Decode GitHub's line-wrapped base64 without accepting corrupt content."""
    if not isinstance(content, str):
        raise TypeError("GitHub content must be a base64 string")
    return base64.b64decode("".join(content.split()), validate=True)


class ModelGrantFetch(BaseModel):
    """The durable bytes the resolver read from the grant anchor.

    ``source_commit_sha`` is the ``main`` commit the file was read at, so the
    promotion decision is reproducible from that exact governance state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: bytes = Field(..., description="Exact grant-file bytes fetched from main.")
    source_commit_sha: str = Field(
        ..., min_length=1, description="omninode_infra@main commit of the file."
    )
    codeowners_match: bool = Field(
        ..., description="Whether the grant file is CODEOWNERS-protected on main."
    )


class ProtocolGrantFetcher(Protocol):
    """The I/O boundary that fetches the grant file from the durable anchor.

    Injected so the EFFECT is testable without network: tests supply a fetcher
    returning fixed bytes; the deployed boundary fetches from
    ``omninode_infra@main`` via the GitHub contents API.
    """

    async def fetch(self) -> ModelGrantFetch:
        """Fetch the grant file bytes + source commit + CODEOWNERS-match from main."""
        ...


class GitHubMainGrantFetcher:
    """Default fetcher: reads the grant file from omninode_infra@main.

    Mirrors ``reject-deploy-gate-skip.yml`` — the file is fetched at
    ``?ref=main`` (anti-self-approval), never from the request's branch. Uses the
    GitHub contents API with the contract-resolved token. This is the EFFECT
    node's canonical I/O boundary (no subprocess, no git shell-out).
    """

    def __init__(self, token: str) -> None:
        self._token = token
        # Preserve each completed read boundary if a subsequent request fails.
        self._provenance = ModelGrantProvenance(codeowners_match=False)

    @property
    def provenance(self) -> ModelGrantProvenance:
        """Audit evidence retained through the last completed read boundary."""
        return self._provenance

    def _request(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", _GITHUB_API_VERSION)
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
            body: bytes = response.read()
        return body

    def _read(self, url: str, stage: EnumGrantAnchorReadStage) -> bytes:
        """``_request`` with an HTTP failure tagged by the read that failed."""
        try:
            return self._request(url)
        except urllib.error.HTTPError as exc:
            raise GrantAnchorReadError(stage, exc.code) from exc

    def _file_is_codeowners_protected(self, *, ref: str) -> bool:
        """Probe whether the grant file path is CODEOWNERS-protected on a ref.

        The trust property is that the grant file requires a dedicated CODEOWNERS
        rule. We confirm a CODEOWNERS file on the fetched commit names the grant
        path.
        """
        for candidate in _CODEOWNERS_PATHS:
            url = (
                f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/contents/{candidate}?ref={ref}"
            )
            try:
                payload = json.loads(
                    self._read(url, EnumGrantAnchorReadStage.CODEOWNERS).decode("utf-8")
                )
            except GrantAnchorReadError as exc:
                if exc.status == 404:
                    continue
                raise
            content = _decode_content(payload["content"]).decode("utf-8")
            if GRANT_FILE_PATH in content:
                return True
        return False

    async def fetch(self) -> ModelGrantFetch:
        self._provenance = ModelGrantProvenance(codeowners_match=False)
        source_commit_sha = self._resolve_main_commit_sha()
        self._provenance = self._provenance.model_copy(
            update={"source_commit_sha": source_commit_sha}
        )
        url = (
            f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/contents/{GRANT_FILE_PATH}"
            f"?ref={source_commit_sha}"
        )
        payload = json.loads(
            self._read(url, EnumGrantAnchorReadStage.GRANT_FILE).decode("utf-8")
        )
        raw = _decode_content(payload["content"])
        self._provenance = self._provenance.model_copy(
            update={"file_sha256": file_sha256(raw)}
        )
        codeowners_match = self._file_is_codeowners_protected(ref=source_commit_sha)
        self._provenance = self._provenance.model_copy(
            update={"codeowners_match": codeowners_match}
        )
        return ModelGrantFetch(
            raw=raw,
            source_commit_sha=source_commit_sha,
            codeowners_match=codeowners_match,
        )

    def _resolve_main_commit_sha(self) -> str:
        """Resolve the current ``main`` tip commit SHA for provenance."""
        url = f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/commits/{GRANT_FETCH_REF}"
        payload = json.loads(
            self._read(url, EnumGrantAnchorReadStage.MAIN_COMMIT).decode("utf-8")
        )
        sha = payload["sha"]
        if not isinstance(sha, str) or not sha:
            raise ValueError("GitHub commit response requires a non-empty sha string")
        return sha


class HandlerProdPromotionGrantResolver:
    """EFFECT: resolve the prod promotion grant from omninode_infra@main.

    A fetcher may be injected for tests; otherwise the GitHub-main fetcher is
    composed at ``handle()`` time with the token resolved from the contract
    ``api_key_ref``.
    """

    def __init__(self, fetcher: ProtocolGrantFetcher | None = None) -> None:
        self._fetcher = fetcher

    async def handle(
        self, payload: ModelProdPromotionGrantResolveCommand
    ) -> ModelProdPromotionGrantResolvedEvent:
        """Resolve the grant and return the resolved fact + audit provenance."""
        command = payload
        fetcher: ProtocolGrantFetcher | None = None
        fetched: ModelGrantFetch | None = None
        try:
            fetcher_or_refusal = await self._resolve_fetcher(command)
            if isinstance(fetcher_or_refusal, ModelProdPromotionGrantResolvedEvent):
                return fetcher_or_refusal
            fetcher = fetcher_or_refusal
            try:
                fetched = await fetcher.fetch()
            except GrantAnchorReadError as exc:
                kind, reason = _http_refusal(exc.status, exc.stage)
                return self._refuse(
                    command,
                    EnumGrantResolution.UNREADABLE,
                    kind,
                    reason,
                    http_status=exc.status,
                    fetcher=fetcher,
                )
            except urllib.error.HTTPError as exc:
                kind, reason = _http_refusal(exc.code, None)
                return self._refuse(
                    command,
                    EnumGrantResolution.UNREADABLE,
                    kind,
                    reason,
                    http_status=exc.code,
                    fetcher=fetcher,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                return self._refuse(
                    command,
                    EnumGrantResolution.UNREADABLE,
                    EnumGrantAnchorRefusal.TRANSPORT_ERROR,
                    f"anchor read failed: transport error ({type(exc).__name__})",
                    fetcher=fetcher,
                )
            except (KeyError, ValueError, TypeError, binascii.Error) as exc:
                return self._refuse(
                    command,
                    EnumGrantResolution.UNREADABLE,
                    EnumGrantAnchorRefusal.UNEXPECTED_RESPONSE,
                    f"anchor read failed: unexpected GitHub API response ({type(exc).__name__})",
                    fetcher=fetcher,
                )

            try:
                resolution = resolve_grant(
                    fetched.raw,
                    requested_image_digest=command.requested_image_digest,
                    promotion_batch_id=command.promotion_batch_id,
                    requested_by=command.requested_by,
                    evaluated_at=command.evaluated_at,
                )
            except (
                ValueError,
                TypeError,
                KeyError,
                yaml.YAMLError,
                UnicodeDecodeError,
            ) as exc:
                reason = f"grant registry unparseable: {exc}"[:300]
                return self._refuse(
                    command,
                    EnumGrantResolution.UNPARSEABLE,
                    EnumGrantAnchorRefusal.REGISTRY_UNPARSEABLE,
                    " ".join(reason.split()),
                    fetched=fetched,
                )
            provenance = ModelGrantProvenance(
                source_commit_sha=fetched.source_commit_sha,
                grant_file_path=GRANT_FILE_PATH,
                grant_id=resolution.grant_id,
                file_sha256=file_sha256(fetched.raw),
                codeowners_match=fetched.codeowners_match,
            )

            return ModelProdPromotionGrantResolvedEvent(
                correlation_id=command.correlation_id,
                resolution=resolution.outcome,
                grant=resolution.grant
                if resolution.outcome is EnumGrantResolution.RESOLVED
                else None,
                evaluated_at=command.evaluated_at,
                provenance=provenance,
            )
        except Exception as exc:
            return self._refuse(
                command,
                EnumGrantResolution.UNREADABLE,
                EnumGrantAnchorRefusal.RESOLVER_ERROR,
                f"resolver error ({type(exc).__name__})",
                fetcher=fetcher,
                fetched=fetched,
            )

    def _refuse(
        self,
        command: ModelProdPromotionGrantResolveCommand,
        resolution: EnumGrantResolution,
        kind: EnumGrantAnchorRefusal,
        reason: str,
        *,
        http_status: int | None = None,
        fetcher: ProtocolGrantFetcher | None = None,
        fetched: ModelGrantFetch | None = None,
    ) -> ModelProdPromotionGrantResolvedEvent:
        """Return an audited refusal on the existing resolved terminal event."""
        provenance = ModelGrantProvenance(
            grant_repo=GRANT_REPO,
            source_ref=GRANT_FETCH_REF,
            grant_file_path=GRANT_FILE_PATH,
            codeowners_match=False,
        )
        if fetched is not None:
            provenance = provenance.model_copy(
                update={
                    "source_commit_sha": fetched.source_commit_sha,
                    "file_sha256": file_sha256(fetched.raw),
                    "codeowners_match": fetched.codeowners_match,
                }
            )
        elif isinstance(fetcher, GitHubMainGrantFetcher):
            provenance = fetcher.provenance
        provenance = provenance.model_copy(
            update={
                "http_status": http_status,
                "refusal_reason": reason,
                "refusal_kind": kind,
            }
        )
        logger.warning(
            "Grant resolver refusal correlation_id=%s resolution=%s kind=%s http_status=%s",
            command.correlation_id,
            resolution.value,
            kind.value,
            http_status,
            extra={
                "correlation_id": str(command.correlation_id),
                "resolution": resolution.value,
                "refusal_kind": kind.value,
                "http_status": http_status,
            },
        )
        return ModelProdPromotionGrantResolvedEvent(
            correlation_id=command.correlation_id,
            resolution=resolution,
            grant=None,
            evaluated_at=command.evaluated_at,
            provenance=provenance,
        )

    async def _resolve_fetcher(
        self, command: ModelProdPromotionGrantResolveCommand
    ) -> ProtocolGrantFetcher | ModelProdPromotionGrantResolvedEvent:
        if self._fetcher is not None:
            return self._fetcher
        token_ref = contract_secret_ref(_CONTRACT_PATH, _ANCHOR_TOKEN_SECRET)
        try:
            secret = await resolve_api_key_async(token_ref)
        except Exception as exc:
            return self._refuse(
                command,
                EnumGrantResolution.UNREADABLE,
                EnumGrantAnchorRefusal.TOKEN_UNRESOLVED,
                f"anchor read token secret ref {token_ref!r} did not resolve "
                f"({type(exc).__name__}); the resolver cannot read the anchor",
            )
        if secret is None or not secret.get_secret_value():
            return self._refuse(
                command,
                EnumGrantResolution.UNREADABLE,
                EnumGrantAnchorRefusal.TOKEN_UNRESOLVED,
                f"anchor read token secret ref {token_ref!r} resolved to no value; "
                "the resolver cannot read the anchor",
            )
        return GitHubMainGrantFetcher(token=secret.get_secret_value())


__all__: list[str] = [
    "EnumGrantAnchorReadStage",
    "GitHubMainGrantFetcher",
    "GrantAnchorReadError",
    "HandlerProdPromotionGrantResolver",
    "ModelGrantFetch",
    "ProtocolGrantFetcher",
]
