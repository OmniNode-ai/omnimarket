# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContractorIntegrationNote — canonical definition-B effect handler.

``handle(request: ModelIntegrationNoteRequest) -> ModelIntegrationNoteResult``.
Typed payload in, typed payload out; no event envelope, no handler-output
wrapper. The runtime's shared local adapter does the envelope work.

The handler owns only sequencing and I/O. Every judgement — is a note owed, what
does it say, has it already been delivered — lives in the pure composer, so the
decision is provable without a Linear key.

Related:
    - OMN-17277: integration note (WS2)
    - OMN-17274: Lakshman customer-plane validation charter (epic)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_request import (
    ModelIntegrationNoteRequest,
    ModelTicketFacts,
)
from omnimarket.nodes.node_contractor_integration_note_effect.models.model_integration_note_result import (
    ModelIntegrationNoteResult,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.adapters import (
    CONTRACT_PATH,
    LINEAR_SECRET_NAME,
    GitReleaseStateProbe,
    ProtocolLinearNoteBoundary,
    ProtocolReleaseStateProbe,
)
from omnimarket.nodes.node_contractor_integration_note_effect.services.note_composer import (
    compose_integration_note,
    extract_ticket_reference,
    parse_note_keys,
)

logger = logging.getLogger(__name__)
_REQUEST_TIMEOUT_SECONDS = 30


def resolve_linear_api_key(contract_path: Path = CONTRACT_PATH) -> str:
    """Resolve the Linear credential through the contract-declared ref."""
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


class HandlerContractorIntegrationNote:
    """Post one integration note per merge that a contractor's ticket owns."""

    def __init__(
        self,
        linear: ProtocolLinearNoteBoundary | None = None,
        releases: ProtocolReleaseStateProbe | None = None,
    ) -> None:
        """Both boundaries are injectable, and default to the real ones.

        Optional rather than required so the runtime's boot resolver can
        construct this handler from the three known-injectable providers alone
        (OMN-13551): a required, default-less parameter makes the handler
        unresolvable and it is quarantined behind a boot warning nobody reads.
        The default is not a degraded stand-in — it is the live boundary, built
        from the contract-declared secret, and it fails closed if that secret is
        unset. Tests and dry runs inject fakes instead.
        """
        self._linear = linear
        self._releases = releases

    def handle(
        self, request: ModelIntegrationNoteRequest
    ) -> ModelIntegrationNoteResult:
        pull_request = request.pull_request
        linear = self._linear or LinearGraphqlNoteBoundary(resolve_linear_api_key())
        releases = self._releases or GitReleaseStateProbe(request.checkout_path)

        ticket_ref = extract_ticket_reference(pull_request)
        ticket = linear.fetch_ticket(ticket_ref) if ticket_ref else None

        # The release probe and the comment read are only worth their cost once
        # the merge is known to belong to a contractor's ticket. Ordering them
        # after the cheap resolution keeps the common case — a merge nobody is
        # waiting on — down to a single Linear read.
        release_tags: tuple[str, ...] = ()
        existing_keys: tuple[str, ...] = ()
        if ticket is not None:
            release_tags = releases.tags_containing(pull_request.merge_sha)
            existing_keys = linear.existing_note_keys(ticket.issue_id)

        decision = compose_integration_note(
            pull_request=pull_request,
            ticket=ticket,
            roster=request.roster,
            release_tags=release_tags,
            existing_note_keys=existing_keys,
        )

        posted = False
        if decision.should_post and not request.dry_run:
            assert ticket is not None  # should_post implies a resolved ticket
            linear.post_note(ticket.issue_id, decision.note_body)
            posted = True
            logger.info(
                "integration note posted: ticket=%s key=%s reachability=%s",
                decision.ticket_identifier,
                decision.note_key,
                decision.reachability,
            )
        elif not decision.should_post:
            logger.info(
                "no integration note owed: key=%s reason=%s",
                decision.note_key,
                decision.skip_reason,
            )

        return ModelIntegrationNoteResult(
            repo=pull_request.repo,
            pr_number=pull_request.number,
            decision=decision,
            posted=posted,
            dry_run=request.dry_run,
        )


__all__ = [
    "HandlerContractorIntegrationNote",
    "LinearGraphqlNoteBoundary",
    "resolve_linear_api_key",
]
