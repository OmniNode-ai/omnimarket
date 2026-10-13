# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for GitHub PR API polling — core logic for node_github_pr_poller_effect.

Implements the ``github.poll.prs`` operation declared in contract.yaml.

Triage State Logic
------------------
``compute_triage_state(pr, stale_hours)`` is a **pure, deterministic function**
that maps a raw GitHub PR payload dict to one of 8 triage states:

    draft | stale | ci_failing | changes_requested | ready_to_merge |
    approved_pending_ci | needs_review | blocked

Evaluation order (first match wins):
    1. draft           — ``pr["draft"] is True``
    2. blocked         — any label in {"blocked", "do-not-merge", "wip"}
    3. stale           — updated_at older than ``stale_hours``
    4. ci_failing      — ``combined_status == "failure"``
    5. changes_requested — at least one reviewer requested changes
    6. ready_to_merge  — CI passing AND at least 1 approval AND no changes_requested
    7. approved_pending_ci — at least 1 approval but CI still pending/running
    8. needs_review    — fallback (no reviews yet)

Non-blocking Design
-------------------
The handler uses the one omnimarket ``GitHubHttpTransport``. GitHub API errors are logged and surfaced in
``ModelGitHubPollerResult.errors`` rather than raising — the poller must not
block the runtime tick loop.

Handler Purity
--------------
The handler does NOT publish events directly. Instead it returns
``ModelGitHubPollerResult.pending_events`` — a list of event payloads
for the node shell / runtime to publish. This follows the ONEX contract
that handlers must not access the event bus.

Related Tickets:
    - OMN-2656: Phase 2 — Effect Nodes & CLIs (omnibase_infra)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import yaml
from omnibase_core.types import JsonType
from omnibase_infra.enums import (
    EnumHandlerType,
    EnumHandlerTypeCategory,
    EnumInfraTransportType,
)
from omnibase_infra.models.errors.model_infra_error_context import (
    ModelInfraErrorContext,
)
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from omnibase_infra.utils import sanitize_error_string

from omnimarket.events.topics import GITHUB_PR_STATUS_TOPIC_V1
from omnimarket.github_api import GitHubHttpTransport
from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_config import (
    ModelGitHubPollerConfig,
)
from omnimarket.nodes.node_github_pr_poller_effect.models.model_github_poller_result import (
    ModelGitHubPollerResult,
)

logger = logging.getLogger(__name__)


class ProtocolGitHubTriageClient(Protocol):
    """Subset of GitHubHttpTransport used by the PR poller."""

    def fetch_open_prs_for_triage(self, repo: str) -> list[dict[str, object]]:
        """Fetch open PR payloads with triage fields."""


# Triage state type alias (matches TriageState in omnibase_core PR model)
TriageState = str


def _coerce_correlation_id(value: object) -> UUID | None:
    """Coerce an incoming correlation_id of unknown type into a UUID."""
    if isinstance(value, UUID):
        return value
    if isinstance(value, str) and value:
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


_BLOCKING_LABELS = frozenset({"blocked", "do-not-merge", "wip"})
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

__all__ = ["HandlerGitHubApiPoll", "compute_triage_state"]


def compute_triage_state(
    pr: dict[str, JsonType],
    stale_hours: int = 48,
) -> TriageState:
    """Compute the triage state for a single GitHub PR payload.

    This is a **pure, deterministic function**. It does not call any external
    service; all required data must be present in ``pr``.

    The ``pr`` dict is expected to mirror the GitHub REST API
    ``GET /repos/{owner}/{repo}/pulls/{pull_number}`` response, augmented
    with:
        - ``pr["combined_status"]`` — string: "success" | "failure" | "pending"
          (derived from the commits statuses/check-runs endpoint by the caller)
        - ``pr["review_states"]`` — list[str]: review states from
          ``GET /repos/{owner}/{repo}/pulls/{pull_number}/reviews``
          (e.g. ["APPROVED", "CHANGES_REQUESTED"])

    Evaluation order (first-match wins):
        1. draft
        2. blocked  (label-based)
        3. stale    (time-based)
        4. ci_failing
        5. changes_requested
        6. ready_to_merge
        7. approved_pending_ci
        8. needs_review (fallback)

    Args:
        pr: GitHub PR payload dict (see above).
        stale_hours: Hours of inactivity that qualify as stale.

    Returns:
        One of the 8 triage state strings.
    """
    # 1. draft
    if pr.get("draft") is True:
        return "draft"

    # 2. blocked — any blocking label present
    raw_labels = pr.get("labels", [])
    labels = raw_labels if isinstance(raw_labels, list) else []
    label_names = {
        str(lbl.get("name", "")).lower() for lbl in labels if isinstance(lbl, dict)
    }
    if label_names & _BLOCKING_LABELS:
        return "blocked"

    # 3. stale — no activity in stale_hours
    updated_at_str = pr.get("updated_at")
    if isinstance(updated_at_str, str):
        try:
            updated_at = datetime.fromisoformat(updated_at_str.rstrip("Z")).replace(
                tzinfo=UTC
            )
            if datetime.now(tz=UTC) - updated_at > timedelta(hours=stale_hours):
                return "stale"
        except ValueError:
            pass

    combined_status = pr.get("combined_status", "pending")
    raw_review_states = pr.get("review_states", [])
    review_states: list[str] = (
        [str(s) for s in raw_review_states]
        if isinstance(raw_review_states, list)
        else []
    )
    has_approval = any(s == "APPROVED" for s in review_states)
    has_changes_requested = any(s == "CHANGES_REQUESTED" for s in review_states)

    # 4. ci_failing
    if combined_status == "failure":
        return "ci_failing"

    # 5. changes_requested
    if has_changes_requested:
        return "changes_requested"

    ci_passing = combined_status == "success"

    # 6. ready_to_merge — CI green + approved + no changes requested
    if ci_passing and has_approval:
        return "ready_to_merge"

    # 7. approved_pending_ci — approved but CI not yet green
    if has_approval:
        return "approved_pending_ci"

    # 8. needs_review (fallback)
    return "needs_review"


def _load_contract_config() -> ModelGitHubPollerConfig:
    """Load the poller config from this node's contract.yaml."""
    with _CONTRACT_PATH.open(encoding="utf-8") as contract_file:
        raw = yaml.safe_load(contract_file)
    if not isinstance(raw, dict):
        raise ValueError("GitHub PR poller contract.yaml must contain a mapping")
    config_raw = raw.get("config") or {}
    if not isinstance(config_raw, dict):
        raise ValueError("GitHub PR poller contract config must be a mapping")
    return ModelGitHubPollerConfig.model_validate(config_raw)


async def _resolve_github_token() -> str:
    """Resolve the GitHub token at the effect boundary from the contract ``secrets`` block."""
    ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
    secret = await resolve_api_key_async(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — ensure GITHUB_TOKEN is set in the secret store."
        )
    return secret.get_secret_value()


class HandlerGitHubApiPoll:
    """Handler for the ``github.poll.prs`` operation.

    Fetches open PRs from the GitHub REST API for each configured repository,
    classifies their triage state via ``compute_triage_state``, and returns
    event payloads in ``ModelGitHubPollerResult.pending_events`` for the
    node shell / runtime to publish.

    Handler Purity:
        This handler does NOT publish events directly. All event payloads are
        returned in ``ModelGitHubPollerResult.pending_events`` for the runtime
        to publish. Handlers must not access the event bus directly.

    Throttling:
        The handler tracks the last poll time per repo (``self._last_polled``)
        and skips repos whose ``poll_interval_seconds`` has not elapsed yet.

    Non-blocking contract:
        Errors from individual repo/PR lookups are collected in
        ``ModelGitHubPollerResult.errors`` (sanitized) rather than raised.
        The handler always returns a result, even on partial failure.

    Args:
        publish_topic: Override of the contract-declared publish topic.
        github_token: Resolved token; when absent it is resolved from the
            contract-declared ``GITHUB_TOKEN`` secret on the first poll that
            has repositories to read.
        github_client_factory: Builds the triage client from the token.
        config: Poller configuration; defaults to the contract ``config``.
    """

    @property
    def handler_type(self) -> EnumHandlerType:
        """Return the architectural role: NODE_HANDLER (bound to PR poller effect node)."""
        return EnumHandlerType.NODE_HANDLER

    @property
    def handler_category(self) -> EnumHandlerTypeCategory:
        """Return the behavioral classification: EFFECT (GitHub REST API I/O)."""
        return EnumHandlerTypeCategory.EFFECT

    # Topic declared in contract.yaml event_bus.publish_topics
    _DEFAULT_PUBLISH_TOPIC = GITHUB_PR_STATUS_TOPIC_V1

    def __init__(
        self,
        publish_topic: str | None = None,
        github_token: str | None = None,
        github_client_factory: Callable[[str], ProtocolGitHubTriageClient]
        | None = None,
        config: ModelGitHubPollerConfig | None = None,
    ) -> None:
        self._github_token = github_token
        self._github_client_factory = github_client_factory or GitHubHttpTransport
        # Topic from contract.yaml; falls back to class default
        self._publish_topic = publish_topic or self._DEFAULT_PUBLISH_TOPIC
        self._config = config or _load_contract_config()
        # Per-repo throttle tracker — maps repo identifier to last poll time
        self._last_polled: dict[str, datetime] = {}

    async def handle(self, request: ModelRuntimeTick) -> ModelGitHubPollerResult:
        """Execute one poll cycle for all configured repositories.

        Repos whose ``poll_interval_seconds`` has not elapsed since the last
        successful poll are skipped silently.

        Args:
            request: The runtime tick that scheduled this cycle. The poller
                configuration is the contract-loaded configuration captured at
                construction time; a tick carries none.

        Returns:
            ``ModelGitHubPollerResult`` with counts, pending events, and any
            non-fatal errors.
        """
        config = self._config
        incoming_correlation_id = _coerce_correlation_id(request.correlation_id)

        errors: list[str] = []
        pending_events: list[JsonType] = []
        repos_polled: list[str] = []
        prs_polled = 0
        now = datetime.now(tz=UTC)
        interval = timedelta(seconds=max(0, config.poll_interval_seconds))

        if not config.repos:
            return ModelGitHubPollerResult(
                events_published=0,
                repos_polled=repos_polled,
                prs_polled=prs_polled,
                errors=errors,
                pending_events=pending_events,
            )

        try:
            token = self._github_token or await _resolve_github_token()
            client = self._github_client_factory(token)
        except Exception as exc:
            error_ctx = ModelInfraErrorContext.with_correlation(
                correlation_id=incoming_correlation_id,
                transport_type=EnumInfraTransportType.RUNTIME,
                operation="init_github_client",
                target_name="github_http_client",
            )
            sanitized = sanitize_error_string(
                f"Error initializing GitHub client: {type(exc).__name__} "
                f"[correlation_id={error_ctx.correlation_id}]"
            )
            logger.warning("%s", sanitized)
            errors.append(sanitized)
            return ModelGitHubPollerResult(
                events_published=0,
                repos_polled=repos_polled,
                prs_polled=prs_polled,
                errors=errors,
                pending_events=pending_events,
            )

        for repo in config.repos:
            # Throttle: skip if interval has not elapsed
            last = self._last_polled.get(repo)
            if last is not None and (now - last) < interval:
                continue

            try:
                pr_events, repo_prs, repo_errors = await self._poll_repo(
                    client=client,
                    repo=repo,
                    stale_hours=config.stale_threshold_hours,
                    correlation_id=incoming_correlation_id,
                )
                self._last_polled[repo] = datetime.now(tz=UTC)
                repos_polled.append(repo)
                prs_polled += repo_prs
                errors.extend(repo_errors)
                pending_events.extend(pr_events)
            except Exception as exc:
                error_ctx = ModelInfraErrorContext.with_correlation(
                    correlation_id=incoming_correlation_id,
                    transport_type=EnumInfraTransportType.RUNTIME,
                    operation="poll_repo",
                    target_name=repo,
                )
                sanitized = sanitize_error_string(
                    f"Error polling repo {repo}: {type(exc).__name__} "
                    f"[correlation_id={error_ctx.correlation_id}]"
                )
                logger.warning("%s", sanitized)
                errors.append(sanitized)

        return ModelGitHubPollerResult(
            events_published=0,  # Runtime publishes from pending_events
            repos_polled=repos_polled,
            prs_polled=prs_polled,
            errors=errors,
            pending_events=pending_events,
        )

    async def _poll_repo(
        self,
        client: ProtocolGitHubTriageClient,
        repo: str,
        stale_hours: int,
        correlation_id: UUID | None = None,
    ) -> tuple[list[JsonType], int, list[str]]:
        """Poll all open PRs for a single repository with pagination.

        Returns:
            Tuple of (event_payloads, total_prs_polled, errors).
        """
        errors: list[str] = []
        pr_events: list[JsonType] = []
        all_prs = await asyncio.to_thread(client.fetch_open_prs_for_triage, repo)

        for pr in all_prs:
            pr_number = pr["number"]
            if not isinstance(pr_number, int):
                continue
            try:
                triage = compute_triage_state(
                    cast("dict[str, JsonType]", pr), stale_hours
                )
                # Topic declared in contract.yaml event_bus.publish_topics
                event_payload: JsonType = {
                    "event_type": self._publish_topic,
                    "repo": repo,
                    "pr_number": pr_number,
                    "triage_state": triage,
                    "title": str(pr.get("title", "")),
                    # is_draft mirrors the pr["draft"] read already used by
                    # compute_triage_state above -- OMN-14394 seam gap fix,
                    # matches ModelOpenPrSummary.is_draft (omnimarket reader).
                    "is_draft": pr.get("draft") is True,
                    "partition_key": f"{repo}:{pr_number}",
                }
                pr_events.append(event_payload)
            except Exception as exc:
                error_ctx = ModelInfraErrorContext.with_correlation(
                    correlation_id=correlation_id,
                    transport_type=EnumInfraTransportType.RUNTIME,
                    operation="process_pr",
                    target_name=f"{repo}#{pr_number}",
                )
                sanitized = sanitize_error_string(
                    f"Error processing PR {repo}#{pr_number}: {type(exc).__name__} "
                    f"[correlation_id={error_ctx.correlation_id}]"
                )
                logger.warning("%s", sanitized)
                errors.append(sanitized)

        return pr_events, len(all_prs), errors
