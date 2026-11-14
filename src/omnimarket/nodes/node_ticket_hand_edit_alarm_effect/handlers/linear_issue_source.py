# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear GraphQL reads behind ProtocolLinearIssueSource.

Same embedded-transport pattern as the ticket node's gateway (each Linear-touching node keeps its own
small transport). The key is resolved on first use from the contract-declared ref, and the endpoint
from the contract's integrations block, so an absent key or endpoint is a read failure the handler
reports as detector_unreadable, never a wiring failure. The history queries are the ones in
``omnimarket.occ_creation_revision``, which holds them without performing I/O.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.inference.secret_store_resolver import resolve_api_key_loop_safe
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
)
from omnimarket.occ_creation_revision import HISTORY_QUERY, ISSUE_QUERY

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"
_KEY_SECRET: Final = "LINEAR_API_KEY"
_PAGE_SIZE: Final = 100
_MAX_PAGES: Final = 20
_TIMEOUT_SECONDS: Final = 30

_CHANGED_QUERY: Final = """
query($after: DateTimeOrDuration!, $first: Int!, $cursor: String) {
  issues(
    first: $first, after: $cursor, orderBy: updatedAt,
    filter: { updatedAt: { gt: $after } }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes { id identifier title description updatedAt }
  }
}
"""


class LinearIssueSourceError(RuntimeError):
    """Linear could not be read or answered something this node cannot decode."""


def _linear_url() -> str:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    return service_endpoint_url(raw["integrations"]["linear"]["graphql_url_ref"])


def parse_timestamp(raw: object) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise LinearIssueSourceError(f"unreadable timestamp {raw!r}")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LinearIssueSourceError(f"unreadable timestamp {raw!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def decode_issue(node: object) -> ModelLinearIssueSnapshot:
    if not isinstance(node, dict):
        raise LinearIssueSourceError(f"an issue is not an object: {node!r}")
    identifier, uuid = node.get("identifier"), node.get("id")
    if not isinstance(identifier, str) or not isinstance(uuid, str):
        raise LinearIssueSourceError("an issue carries no id or identifier")
    description = node.get("description")
    title = node.get("title")
    return ModelLinearIssueSnapshot(
        ticket_id=identifier,
        issue_uuid=uuid,
        title=title if isinstance(title, str) else "",
        description=description if isinstance(description, str) else "",
        updated_at=parse_timestamp(node.get("updatedAt")),
    )


def decode_revisions(history: object) -> list[ModelDescriptionRevision]:
    """Every entry must decode: a revision that cannot be read is not read as unedited."""
    if not isinstance(history, list):
        raise LinearIssueSourceError("the description history is not a list")
    revisions = []
    for entry in history:
        if not isinstance(entry, dict):
            raise LinearIssueSourceError(f"a history entry is not an object: {entry!r}")
        actors = entry.get("actorIds")
        revisions.append(
            ModelDescriptionRevision(
                snapshot_at=parse_timestamp(entry.get("contentDataSnapshotAt")),
                actor_ids=tuple(a for a in actors if isinstance(a, str))
                if isinstance(actors, list)
                else (),
            )
        )
    return revisions


class LinearIssueSourceGateway:
    """Reads changed issues and description revisions; never writes."""

    def __init__(self) -> None:
        self._key: str | None = None
        self._url: str | None = None

    def _post(self, query: str, variables: dict[str, object]) -> dict[str, Any]:
        if self._key is None:
            ref = contract_secret_ref(_CONTRACT_PATH, _KEY_SECRET)
            secret = resolve_api_key_loop_safe(ref, env_var_fallback=ref)
            if secret is None or not secret.get_secret_value():
                raise LinearIssueSourceError(f"secret ref {ref!r} resolved to nothing")
            self._key = secret.get_secret_value()
        if self._url is None:
            self._url = _linear_url()
        request = urllib.request.Request(
            self._url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={"Content-Type": "application/json", "Authorization": self._key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read())
        except (OSError, ValueError) as exc:
            raise LinearIssueSourceError(f"Linear read failed: {exc}") from exc
        if not isinstance(body, dict) or body.get("errors") or "data" not in body:
            raise LinearIssueSourceError(
                f"Linear answered an error: {body.get('errors') if isinstance(body, dict) else body!r}"
            )
        data = body["data"]
        if not isinstance(data, dict):
            raise LinearIssueSourceError("Linear answered no data")
        return data

    def list_issues_updated_after(
        self, updated_after: datetime
    ) -> list[ModelLinearIssueSnapshot]:
        issues: list[ModelLinearIssueSnapshot] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._post(
                _CHANGED_QUERY,
                {
                    "after": updated_after.astimezone(UTC).isoformat(),
                    "first": _PAGE_SIZE,
                    "cursor": cursor,
                },
            ).get("issues")
            if not isinstance(page, dict) or not isinstance(page.get("nodes"), list):
                raise LinearIssueSourceError("Linear answered no issue page")
            issues.extend(decode_issue(node) for node in page["nodes"])
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return issues
            cursor = info.get("endCursor")
        raise LinearIssueSourceError(
            f"more than {_MAX_PAGES} pages changed since {updated_after.isoformat()}; the cursor stays"
        )

    def description_revisions(
        self, issue: ModelLinearIssueSnapshot
    ) -> list[ModelDescriptionRevision]:
        document = (
            self._post(ISSUE_QUERY, {"id": issue.ticket_id}).get("issue") or {}
        ).get("documentContent")
        if not isinstance(document, dict) or not document.get("id"):
            raise LinearIssueSourceError(
                f"{issue.ticket_id} has no description document to read the history of"
            )
        container = self._post(HISTORY_QUERY, {"id": str(document["id"])}).get(
            "documentContentHistory"
        )
        if not isinstance(container, dict):
            raise LinearIssueSourceError(f"{issue.ticket_id} history is unreadable")
        return decode_revisions(container.get("history"))


__all__ = [
    "LinearIssueSourceError",
    "LinearIssueSourceGateway",
    "decode_issue",
    "decode_revisions",
]
