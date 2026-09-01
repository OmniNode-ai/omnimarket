# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Effect-boundary adapters for the contractor integration note (OMN-17277).

Two boundaries, each behind a Protocol so the handler stays testable with no
network and no checkout:

``ProtocolLinearNoteBoundary`` — read the ticket, read the notes already on it,
write one comment. ``LinearGraphqlNoteBoundary`` is its one implementation.

``ProtocolReleaseStateProbe`` — answer "is this merge commit in a released tag",
which is the difference between "you can use it now" and "here is the pin you
need". Answered from git, because the tag graph is the authority on it; a
release note or a version file states an intent, the tag states the fact.

The Linear credential is never a bare literal here: the ref name is read from
the node's own ``contract.yaml`` ``secrets`` block via ``contract_secret_ref``,
and the value is read from the process environment the secret store populates.
The endpoint is likewise resolved from the service-endpoint authority
(``configs/service_endpoints.yaml``), not written as a URL literal.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-12856: contract-declared secret refs
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Protocol

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    parse_note_keys,
)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
LINEAR_SECRET_NAME = "LINEAR_API_KEY"
_REQUEST_TIMEOUT_SECONDS = 30


class ProtocolLinearNoteBoundary(Protocol):
    """Effect boundary for the Linear side of an integration note."""

    def fetch_ticket(self, identifier: str) -> ModelTicketFacts | None:
        """Resolve a ticket key (``OMN-123``) to its id and assignee."""

    def existing_note_keys(self, issue_id: str) -> tuple[str, ...]:
        """Return the integration-note keys already posted on this ticket."""

    def post_note(self, issue_id: str, body: str) -> None:
        """Write one comment carrying the note."""


class ProtocolReleaseStateProbe(Protocol):
    """Effect boundary for "is this commit in a released tag"."""

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        """Return the release tags whose history contains ``merge_sha``."""


def resolve_linear_api_key(contract_path: Path = CONTRACT_PATH) -> str:
    """Resolve the Linear credential through the contract-declared ref.

    Fails closed and loudly: an unset credential means the note cannot be
    delivered, and a node that exits 0 on an undelivered note reproduces the
    exact silence this node was built to end.
    """
    ref = contract_secret_ref(contract_path, LINEAR_SECRET_NAME)
    value = os.environ.get(ref)
    if not value:
        raise RuntimeError(
            f"Secret {ref!r} is declared in {contract_path.name} but is unset in "
            "the environment; refusing to run — an undelivered note must not "
            "read as a successful one."
        )
    return value


class LinearGraphqlNoteBoundary:
    """The Linear GraphQL implementation of the note boundary."""

    def __init__(self, api_key: str, *, endpoint: str = LINEAR_GRAPHQL_URL) -> None:
        self._api_key = api_key
        self._endpoint = endpoint

    def _post(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        payload = json.dumps({"query": query, "variables": variables}).encode()
        request = urllib.request.Request(
            self._endpoint,
            data=payload,
            headers={
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(
            request, timeout=_REQUEST_TIMEOUT_SECONDS
        ) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise RuntimeError("Linear GraphQL returned a non-object response")
        if data.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data

    def fetch_ticket(self, identifier: str) -> ModelTicketFacts | None:
        query = """
        query IntegrationNoteTicket($id: String!) {
          issue(id: $id) {
            id
            identifier
            title
            assignee { id }
          }
        }
        """
        try:
            data = self._post(query, {"id": identifier})
        except (urllib.error.HTTPError, RuntimeError):
            return None
        issue = (data.get("data") or {}).get("issue")
        if not isinstance(issue, dict):
            return None
        assignee = issue.get("assignee") or {}
        return ModelTicketFacts(
            issue_id=str(issue["id"]),
            identifier=str(issue["identifier"]),
            title=str(issue.get("title") or ""),
            assignee_linear_user_id=(
                str(assignee["id"]) if isinstance(assignee, dict) and assignee else None
            ),
        )

    def existing_note_keys(self, issue_id: str) -> tuple[str, ...]:
        query = """
        query IntegrationNoteComments($id: String!) {
          issue(id: $id) {
            comments(first: 250) { nodes { body } }
          }
        }
        """
        data = self._post(query, {"id": issue_id})
        issue = (data.get("data") or {}).get("issue") or {}
        nodes = ((issue.get("comments") or {}).get("nodes")) or []
        return parse_note_keys(
            str(node.get("body") or "") for node in nodes if isinstance(node, dict)
        )

    def post_note(self, issue_id: str, body: str) -> None:
        query = """
        mutation IntegrationNoteCreate($issueId: String!, $body: String!) {
          commentCreate(input: { issueId: $issueId, body: $body }) { success }
        }
        """
        data = self._post(query, {"issueId": issue_id, "body": body})
        success = ((data.get("data") or {}).get("commentCreate") or {}).get(
            "success"
        ) is True
        if not success:
            raise RuntimeError(f"Linear refused the integration note for {issue_id}")


class GitReleaseStateProbe:
    """Concrete ``ProtocolReleaseStateProbe`` over ``git tag --contains``.

    ``repo_path`` is supplied by the caller (the workflow's own checkout). No
    default is inferred from the environment: guessing a repository root is how
    a probe ends up answering about the wrong tree, and "no tags found" then
    silently reads as "not released".
    """

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path

    def tags_containing(self, merge_sha: str) -> tuple[str, ...]:
        completed = subprocess.run(
            ["git", "-C", str(self._repo_path), "tag", "--contains", merge_sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "git tag --contains failed for "
                f"{merge_sha} in {self._repo_path}: {completed.stderr.strip()}"
            )
        return tuple(
            line.strip() for line in completed.stdout.splitlines() if line.strip()
        )


__all__ = [
    "CONTRACT_PATH",
    "LINEAR_SECRET_NAME",
    "GitReleaseStateProbe",
    "LinearGraphqlNoteBoundary",
    "ProtocolLinearNoteBoundary",
    "ProtocolReleaseStateProbe",
    "resolve_linear_api_key",
]
