# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_prod_promotion_grant_resolver_effect (OMN-13439 / Phase 2b).

EFFECT node. The orchestrator fact-gathering boundary that resolves the prod
promotion grant from the durable trust anchor BEFORE the prod gate evaluates.

Anti-self-approval (OMN-10971): the grant is fetched from
``onex_change_control@main`` — NOT a PR branch — exactly as
``reject-deploy-gate-skip.yml`` fetches its skip-token allowlist
(``contents/<path>?ref=main``). A redeploy request therefore cannot author the
authorization that approves it, even by editing the grant file in the same change.

The handler:
  1. resolves the GitHub token from the contract ``api_key_ref`` at the effect
     boundary (no bare ``os.environ`` read, no subprocess shell-out);
  2. fetches the grant file bytes + source commit SHA from ``main`` and probes
     whether the file is CODEOWNERS-protected on that ref;
  3. parses the YAML directly (ZERO Python import on onex_change_control) and
     resolves it against the request key via the pure ``grant_resolver``;
  4. emits ``ModelProdPromotionGrantResolvedEvent`` carrying the resolved grant
     (or ``None`` — fail closed) plus durable audit provenance.

Provenance lives on the EMITTED AUDIT EVIDENCE, never on the pure grant DTO.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import (
    GRANT_FETCH_REF,
    GRANT_FILE_PATH,
    GRANT_REPO,
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

_HANDLER_ID = "node_prod_promotion_grant_resolver_effect"
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# GitHub API host for the grant-anchor read. This is the public api.github.com
# control-plane host (no per-model routing authority applies to a VCS read of the
# governance anchor), matching node_github_review_effect's identical I/O boundary.
_GITHUB_API_BASE = "https://api.github.com"  # url-authority-ok: GitHub control-plane host for the onex_change_control@main grant read; no model routing authority
_GITHUB_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT = 30.0
_CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


class ModelGrantFetch(BaseModel):
    """The durable bytes the resolver read from the grant anchor.

    ``source_commit_sha`` is the ``main`` commit the file was read at, so the
    promotion decision is reproducible from that exact governance state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    raw: bytes = Field(..., description="Exact grant-file bytes fetched from main.")
    source_commit_sha: str = Field(
        ..., min_length=1, description="onex_change_control@main commit of the file."
    )
    codeowners_match: bool = Field(
        ..., description="Whether the grant file is CODEOWNERS-protected on main."
    )


class ProtocolGrantFetcher(Protocol):
    """The I/O boundary that fetches the grant file from the durable anchor.

    Injected so the EFFECT is testable without network: tests supply a fetcher
    returning fixed bytes; the deployed boundary fetches from
    ``onex_change_control@main`` via the GitHub contents API.
    """

    async def fetch(self) -> ModelGrantFetch:
        """Fetch the grant file bytes + source commit + CODEOWNERS-match from main."""
        ...


class GitHubMainGrantFetcher:
    """Default fetcher: reads the grant file from onex_change_control@main.

    Mirrors ``reject-deploy-gate-skip.yml`` — the file is fetched at
    ``?ref=main`` (anti-self-approval), never from the request's branch. Uses the
    GitHub contents API with the contract-resolved token. This is the EFFECT
    node's canonical I/O boundary (no subprocess, no git shell-out).
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def _request(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", _GITHUB_API_VERSION)
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT) as response:
            body: bytes = response.read()
        return body

    def _file_is_codeowners_protected(self) -> bool:
        """Probe whether the grant file path is CODEOWNERS-protected on main.

        The trust property is that the grant file requires a dedicated CODEOWNERS
        rule. We confirm a CODEOWNERS file on main names the grant path.
        """
        for candidate in _CODEOWNERS_PATHS:
            url = (
                f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/contents/{candidate}"
                f"?ref={GRANT_FETCH_REF}"
            )
            try:
                payload = json.loads(self._request(url).decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                raise
            content = base64.b64decode(payload["content"]).decode("utf-8")
            if GRANT_FILE_PATH in content:
                return True
        return False

    async def fetch(self) -> ModelGrantFetch:
        url = (
            f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/contents/{GRANT_FILE_PATH}"
            f"?ref={GRANT_FETCH_REF}"
        )
        payload = json.loads(self._request(url).decode("utf-8"))
        raw = base64.b64decode(payload["content"])
        source_commit_sha = self._resolve_main_commit_sha()
        return ModelGrantFetch(
            raw=raw,
            source_commit_sha=source_commit_sha,
            codeowners_match=self._file_is_codeowners_protected(),
        )

    def _resolve_main_commit_sha(self) -> str:
        """Resolve the current ``main`` tip commit SHA for provenance."""
        url = f"{_GITHUB_API_BASE}/repos/{GRANT_REPO}/commits/{GRANT_FETCH_REF}"
        payload = json.loads(self._request(url).decode("utf-8"))
        return str(payload["sha"])


class HandlerProdPromotionGrantResolver:
    """EFFECT: resolve the prod promotion grant from onex_change_control@main.

    A fetcher may be injected for tests; otherwise the GitHub-main fetcher is
    composed at ``handle()`` time with the token resolved from the contract
    ``api_key_ref``.
    """

    def __init__(self, fetcher: ProtocolGrantFetcher | None = None) -> None:
        self._fetcher = fetcher

    async def handle(
        self, command: ModelProdPromotionGrantResolveCommand
    ) -> ModelHandlerOutput[None]:
        """Resolve the grant and emit the resolved fact + audit provenance."""
        fetcher = await self._resolve_fetcher()
        fetched = await fetcher.fetch()

        resolution = resolve_grant(
            fetched.raw,
            requested_image_digest=command.requested_image_digest,
            promotion_batch_id=command.promotion_batch_id,
            requested_by=command.requested_by,
            evaluated_at=command.evaluated_at,
        )

        provenance = ModelGrantProvenance(
            source_commit_sha=fetched.source_commit_sha,
            grant_file_path=GRANT_FILE_PATH,
            grant_id=resolution.grant_id,
            file_sha256=file_sha256(fetched.raw),
            codeowners_match=fetched.codeowners_match,
        )

        event = ModelProdPromotionGrantResolvedEvent(
            correlation_id=command.correlation_id,
            resolution=resolution.outcome,
            grant=resolution.grant
            if resolution.outcome is EnumGrantResolution.RESOLVED
            else None,
            evaluated_at=command.evaluated_at,
            provenance=provenance,
        )
        return ModelHandlerOutput.for_effect(
            input_envelope_id=uuid4(),
            correlation_id=command.correlation_id,
            handler_id=_HANDLER_ID,
            events=(event,),
        )

    async def _resolve_fetcher(self) -> ProtocolGrantFetcher:
        if self._fetcher is not None:
            return self._fetcher
        github_ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
        secret = await resolve_api_key_async(github_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {github_ref!r} resolved to None — "
                "ensure GITHUB_TOKEN is set in the secret store."
            )
        return GitHubMainGrantFetcher(token=secret.get_secret_value())


__all__: list[str] = [
    "GitHubMainGrantFetcher",
    "HandlerProdPromotionGrantResolver",
    "ModelGrantFetch",
    "ProtocolGrantFetcher",
]
