# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRepoHealthRepairEffect — emit durable repo-baseline repair task.

Receives a REPO_BASELINE classification and emits a durable Linear ticket
(under the parent epic OMN-13316) carrying the failing command, baseline
evidence, and the reason the failure is repo-baseline not PR-scoped.

Idempotency guarantee
---------------------
A content key is derived as SHA-256(failing_command + "|" + "|".join(sorted(failing_paths))).
Before creating a new Linear ticket, the handler queries Linear for an existing
issue tagged with this key. If one exists, its ref is returned and ticket_created
is set to False, preventing duplicate tickets across sweep iterations.

Secret resolution
-----------------
The LINEAR_API_KEY is resolved at invocation time from the contract-declared
``api_key_ref`` (``LINEAR_API_KEY``) via the canonical secret-store resolver
(``omnimarket.inference.secret_store_resolver.resolve_api_key``).  The literal
secret value is never present in this source file.

Related:
    - OMN-13584: this node
    - OMN-13316: parent epic (repair tasks attach here)
    - OMN-13027: dev-baseline ratchet (source of dev_baseline_paths in envelope)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_command import (
    ModelRepoHealthRepairCommand,
)
from omnimarket.nodes.node_repo_health_repair_effect.models.model_repair_emitted_event import (
    ModelRepoHealthRepairEmittedEvent,
)

logger = logging.getLogger(__name__)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"

# Linear GraphQL endpoint
_LINEAR_GRAPHQL_URL = "https://api.linear.app/graphql"

# Label applied to all content-key-indexed tickets for search-by-key
_CONTENT_KEY_LABEL_PREFIX = "onex-repair-key:"


# ---------------------------------------------------------------------------
# Injectable protocol for Linear (enables unit testing without network calls)
# ---------------------------------------------------------------------------


@runtime_checkable
class LinearRepairClientProtocol(Protocol):
    """Protocol for the Linear client used by HandlerRepoHealthRepairEffect.

    Both the real HTTP client and the unit-test mock implement this interface.
    """

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        """Return an existing ticket identifier for this key, or None if absent."""
        ...

    def create_issue(self, *, title: str, description: str, parent_id: str) -> str:
        """Create a new Linear issue under parent_id and return its identifier."""
        ...


# ---------------------------------------------------------------------------
# Real Linear HTTP client (GraphQL)
# ---------------------------------------------------------------------------


class LinearRepairHttpClient:
    """Real Linear HTTP client for the repair effect.

    Reads the API key from the caller-supplied resolved secret — never from
    ``os.environ`` directly.  All network interaction is isolated here.
    """

    _BASE = _LINEAR_GRAPHQL_URL

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise RuntimeError(
                "LINEAR_API_KEY must not be empty. "
                "Resolve it from the contract api_key_ref before constructing the client."
            )
        self._api_key = api_key

    def _post(self, query: str, variables: dict[str, object]) -> Any:
        import json
        import urllib.request

        payload = json.dumps({"query": query, "variables": variables}).encode()
        req = urllib.request.Request(
            self._BASE,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if "errors" in data:
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data

    def search_issues_by_content_key(self, *, content_key: str) -> str | None:
        """Search for an existing repair ticket by content key label.

        Linear does not support label-text search directly via GraphQL in a
        single query, so we search issues with a title prefix that includes the
        key.  The title convention is:
            "[onex-repair] <content_key_prefix> <short description>"
        We search by the label prefix and return the first match identifier.
        """
        label_filter = f"{_CONTENT_KEY_LABEL_PREFIX}{content_key}"
        query = """
        query SearchByLabel($filter: String!) {
          issues(
            first: 1,
            filter: { labels: { name: { containsIgnoreCase: $filter } } }
          ) {
            nodes { identifier }
          }
        }
        """
        try:
            data = self._post(query, {"filter": label_filter})
            nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
            if nodes:
                return str(nodes[0]["identifier"])
        except Exception as exc:
            logger.warning(
                "Linear search for content_key %s failed: %s — treating as not found",
                content_key[:16],
                exc,
            )
        return None

    def create_issue(self, *, title: str, description: str, parent_id: str) -> str:
        """Create a new Linear issue and return its identifier."""
        # First resolve the team ID for Omninode
        team_data = self._post(
            """
            query {
              teams(filter: { name: { eq: "Omninode" } }) {
                nodes { id }
              }
            }
            """,
            {},
        )
        nodes = team_data.get("data", {}).get("teams", {}).get("nodes", [])
        if not nodes:
            raise RuntimeError("Linear team 'Omninode' not found")
        team_id = nodes[0]["id"]

        # Resolve parent issue UUID from identifier (e.g. "OMN-13316")
        parent_data = self._post(
            """
            query GetIssueByIdentifier($identifier: String!) {
              issue(id: $identifier) { id }
            }
            """,
            {"identifier": parent_id},
        )
        parent_uuid = parent_data.get("data", {}).get("issue", {}).get("id", "")

        mutation = """
        mutation CreateIssue($teamId: String!, $title: String!, $description: String!, $parentId: String) {
          issueCreate(input: {
            teamId: $teamId,
            title: $title,
            description: $description,
            parentId: $parentId
          }) {
            issue { identifier }
          }
        }
        """
        variables: dict[str, object] = {
            "teamId": team_id,
            "title": title,
            "description": description,
        }
        if parent_uuid:
            variables["parentId"] = parent_uuid

        result = self._post(mutation, variables)
        identifier = str(
            result.get("data", {})
            .get("issueCreate", {})
            .get("issue", {})
            .get("identifier", "")
        )
        if not identifier:
            raise RuntimeError(f"Linear issueCreate returned no identifier: {result}")
        return identifier


# ---------------------------------------------------------------------------
# Content-key derivation (pure function — deterministic)
# ---------------------------------------------------------------------------


def _derive_content_key(failing_command: str, failing_paths: tuple[str, ...]) -> str:
    """Derive a SHA-256 idempotency key from failing_command + sorted failing_paths.

    The key is stable across: repeated sweep runs, different call order of the
    same paths, and any whitespace normalization already applied upstream.

    Args:
        failing_command: The verbatim failing command string.
        failing_paths:   The set of paths implicated by the failure.

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    canonical = failing_command + "|" + "|".join(sorted(failing_paths))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerRepoHealthRepairEffect:
    """EFFECT handler: emits a durable Linear repair task for REPO_BASELINE failures.

    Secret resolution:
        - When ``linear_client`` is None (the default), the handler resolves
          ``LINEAR_API_KEY`` from the contract-declared ``api_key_ref`` via
          ``contract_secret_ref`` + ``resolve_api_key`` and constructs a
          ``LinearRepairHttpClient``.
        - When ``linear_client`` is provided (unit tests), it is used directly,
          bypassing all secret resolution.

    Idempotency:
        A SHA-256 content key derived from (failing_command, sorted failing_paths)
        is looked up in Linear before creating a ticket. If a match is found, the
        pre-existing ref is returned and ``ticket_created`` is False.
    """

    handler_type: str = "NODE_HANDLER"
    handler_category: str = "EFFECT"

    def __init__(
        self,
        linear_client: LinearRepairClientProtocol | None = None,
    ) -> None:
        self._injectable_client = linear_client

    def _get_client(self) -> LinearRepairClientProtocol:
        if self._injectable_client is not None:
            return self._injectable_client
        # Ref-name sourced from contract (not a bare literal).
        _linear_ref = contract_secret_ref(_CONTRACT_PATH, "LINEAR_API_KEY")
        secret = resolve_api_key(_linear_ref)
        if secret is None:
            raise RuntimeError(
                f"api_key_ref {_linear_ref!r} resolved to None — "
                "ensure LINEAR_API_KEY is set in the secret store."
            )
        return LinearRepairHttpClient(secret.get_secret_value())

    async def handle(
        self, command: ModelRepoHealthRepairCommand
    ) -> ModelRepoHealthRepairEmittedEvent:
        """Emit (or idempotently reference) a durable Linear repair task.

        Args:
            command: Inbound repair command carrying the REPO_BASELINE classification.

        Returns:
            ``ModelRepoHealthRepairEmittedEvent`` with the content_key and
            ticket ref (or None in dry_run mode).
        """
        cls = command.classification
        content_key = _derive_content_key(cls.failing_command, cls.matched_paths)

        logger.info(
            "repo-health-repair-effect: correlation_id=%s repo=%s pr=%s "
            "content_key=%s... dry_run=%s",
            command.correlation_id,
            cls.repo,
            cls.pr_number,
            content_key[:16],
            command.dry_run,
        )

        # dry_run: return the computed key without touching Linear.
        if command.dry_run:
            return ModelRepoHealthRepairEmittedEvent(
                correlation_id=command.correlation_id,
                repo=cls.repo,
                pr_number=cls.pr_number,
                failing_command=cls.failing_command,
                classification_reason=cls.reason,
                content_key=content_key,
                repair_ticket_ref=None,
                ticket_created=False,
                dry_run=True,
            )

        client = self._get_client()

        # Idempotency check — avoid duplicate tickets across sweep iterations.
        existing_ref = client.search_issues_by_content_key(content_key=content_key)
        if existing_ref is not None:
            logger.info(
                "repo-health-repair-effect: found existing ticket %s for key %s...",
                existing_ref,
                content_key[:16],
            )
            return ModelRepoHealthRepairEmittedEvent(
                correlation_id=command.correlation_id,
                repo=cls.repo,
                pr_number=cls.pr_number,
                failing_command=cls.failing_command,
                classification_reason=cls.reason,
                content_key=content_key,
                repair_ticket_ref=existing_ref,
                ticket_created=False,
                dry_run=False,
            )

        # Build ticket title and description with required evidence fields.
        short_key = content_key[:8]
        title = (
            f"[onex-repair] Repo-baseline failure: {cls.failing_command!r} "
            f"[{_CONTENT_KEY_LABEL_PREFIX}{content_key}]"
        )
        description = _build_ticket_description(
            classification=cls,
            content_key=content_key,
            parent_issue_id=command.parent_issue_id,
        )

        new_ref = client.create_issue(
            title=title,
            description=description,
            parent_id=command.parent_issue_id,
        )
        logger.info(
            "repo-health-repair-effect: created ticket %s (key %s...)",
            new_ref,
            short_key,
        )

        return ModelRepoHealthRepairEmittedEvent(
            correlation_id=command.correlation_id,
            repo=cls.repo,
            pr_number=cls.pr_number,
            failing_command=cls.failing_command,
            classification_reason=cls.reason,
            content_key=content_key,
            repair_ticket_ref=new_ref,
            ticket_created=True,
            dry_run=False,
        )


def _build_ticket_description(
    *,
    classification: Any,
    content_key: str,
    parent_issue_id: str,
) -> str:
    """Build the durable Linear ticket description with all required evidence fields."""
    paths_str = "\n".join(f"  - `{p}`" for p in classification.matched_paths)
    return (
        f"## Repo-baseline repair task\n\n"
        f"**Parent epic:** {parent_issue_id}\n"
        f"**Content key (idempotency):** `{content_key}`\n"
        f"**Label:** `{_CONTENT_KEY_LABEL_PREFIX}{content_key}`\n\n"
        f"### Failing command\n\n"
        f"```\n{classification.failing_command}\n```\n\n"
        f"### Baseline evidence (classification reason)\n\n"
        f"{classification.reason}\n\n"
        f"### Implicated paths\n\n"
        f"{paths_str if paths_str else '_(none)_'}\n\n"
        f"### Repository\n\n"
        f"`{classification.repo}`"
        + (
            f"\n\n### PR\n\n#{classification.pr_number}"
            if classification.pr_number is not None
            else ""
        )
        + "\n\n---\n"
        "_Auto-emitted by node_repo_health_repair_effect (OMN-13584) "
        "as part of the merge-sweep repo-health repair lane._\n"
    )


__all__ = [
    "HandlerRepoHealthRepairEffect",
    "LinearRepairClientProtocol",
    "LinearRepairHttpClient",
]
