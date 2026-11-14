# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two Linear reads over GraphQL: changed issues, and one description's revisions.

The transport is embedded here, as each Linear-reading node in this repository embeds
its own (see ``node_create_ticket``'s ``LinearTicketHttpGateway``). The history query and
the revision decoding are ``occ_creation_revision``'s, so the editor is named the way the
transcriber names the acceptor. Every failure raises; the handler turns it into
``detector_unreadable`` and leaves the cursor where it was.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
)
from omnimarket.occ_creation_revision import HISTORY_QUERY

ISSUES_UPDATED_QUERY: Final[str] = """
query($after: DateTimeOrDuration!, $cursor: String) {
  issues(
    filter: { updatedAt: { gt: $after } }
    orderBy: updatedAt
    first: 100
    after: $cursor
  ) {
    nodes {
      identifier
      title
      description
      updatedAt
      documentContent { id }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


def _timestamp(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"Linear returned no timestamp: {raw!r}")
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def decode_issues(
    data: dict[str, Any],
) -> tuple[list[ModelLinearIssueSnapshot], str | None]:
    """One page of the list read, and the next page's cursor (None on the last page)."""
    issues = (data.get("data") or {}).get("issues")
    if not isinstance(issues, dict):
        raise ValueError("Linear returned no issues connection")
    page: list[ModelLinearIssueSnapshot] = []
    for node in issues.get("nodes") or []:
        document = node.get("documentContent") or {}
        page.append(
            ModelLinearIssueSnapshot(
                identifier=str(node["identifier"]),
                title=str(node.get("title") or ""),
                description=str(node.get("description") or ""),
                updated_at=_timestamp(node.get("updatedAt")),
                document_content_id=str(document["id"]) if document.get("id") else None,
            )
        )
    info = issues.get("pageInfo") or {}
    return page, (str(info["endCursor"]) if info.get("hasNextPage") else None)


def decode_revisions(data: dict[str, Any]) -> list[ModelDescriptionRevision]:
    """Every revision of a description document; a missing history raises."""
    history = ((data.get("data") or {}).get("documentContentHistory") or {}).get(
        "history"
    )
    if not isinstance(history, list):
        raise ValueError("Linear returned no documentContentHistory")
    return [
        ModelDescriptionRevision(
            snapshot_at=_timestamp(entry.get("contentDataSnapshotAt")),
            actor_ids=tuple(str(actor) for actor in entry.get("actorIds") or ()),
        )
        for entry in history
    ]


class LinearIssueGateway:
    """Reads Linear with the contract-declared key at the contract-declared endpoint."""

    def __init__(
        self, *, api_key: str, graphql_url: str, timeout_s: float = 30
    ) -> None:
        if not api_key:
            raise RuntimeError("the Linear key resolved empty")
        self._api_key = api_key
        self._url = graphql_url
        self._timeout_s = timeout_s

    def _post(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": self._api_key,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise ValueError("Linear returned a non-object reply")
        if data.get("errors"):
            raise RuntimeError(f"Linear GraphQL error: {data['errors']}")
        return data

    def issues_updated_after(
        self, cursor: datetime, *, max_pages: int
    ) -> Sequence[ModelLinearIssueSnapshot]:
        found: list[ModelLinearIssueSnapshot] = []
        page_cursor: str | None = None
        for _ in range(max_pages):
            page, page_cursor = decode_issues(
                self._post(
                    ISSUES_UPDATED_QUERY,
                    {"after": cursor.isoformat(), "cursor": page_cursor},
                )
            )
            found.extend(page)
            if page_cursor is None:
                return found
        raise RuntimeError(
            f"more than {max_pages} pages of issues changed after {cursor.isoformat()}"
        )

    def description_revisions(
        self, document_content_id: str
    ) -> Sequence[ModelDescriptionRevision]:
        return decode_revisions(self._post(HISTORY_QUERY, {"id": document_content_id}))


__all__ = [
    "ISSUES_UPDATED_QUERY",
    "LinearIssueGateway",
    "decode_issues",
    "decode_revisions",
]
