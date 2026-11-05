# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write ``linear-<UTC date>.json``: the signed-in subject's open Linear issues, typed once.

The lab fill generator reads the file for the operator's open tickets and plans-today reads it as the
identity sidecar. Until now only the compact ``.txt`` was written by code; the JSON was a prose step an
agent could skip, and with it absent the tick's ``lab-fill-generator`` line read ``linear_snapshot
unread`` and no ticket was generated. The read resolves its contract-declared key once through the
secret store, pages with the cursor until ``hasNextPage`` is false and
refuses a page limit that would cut the list short. The result carries counts only.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelLinearSnapshotRequest,
    ModelLinearSnapshotResult,
    ModelSnapshotIssue,
)

KEY_ENV = "LINEAR_API_KEY"
_CONTRACT = Path(__file__).parents[1] / "contract.yaml"
HTTP_TIMEOUT_S = 60.0

QUERY = """
query SnapshotIssues($first: Int!, $after: String) {
  viewer { id }
  issues(
    first: $first
    after: $after
    filter: {
      team: { key: { eq: "OMN" } }
      assignee: { isMe: { eq: true } }
      state: { type: { nin: ["completed", "canceled"] } }
    }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      description
      updatedAt
      priority
      priorityLabel
      state { name type }
      assignee { id }
      labels { nodes { name } }
      project { name }
    }
  }
}
"""

#: ``post(query, variables)`` returns the GraphQL ``data`` object; it raises on any transport or GraphQL error.
Post = Callable[[str, dict[str, Any]], dict[str, Any]]


class SnapshotReadError(RuntimeError):
    """The Linear read could not produce a complete snapshot."""

    def __init__(self, message: str, *, code: str = "read_failed") -> None:
        super().__init__(message)
        self.code = code


def resolve_key() -> SecretStr:
    """Resolve once per snapshot at the existing synchronous effect boundary."""
    from omnimarket.inference.secret_store_resolver import (
        SecretStoreConfigurationError,
        resolve_api_key_loop_safe,
    )
    from omnimarket.models.secret_store.model_secret_resolution_error import (
        SecretResolutionError,
    )

    try:
        key = resolve_api_key_loop_safe(contract_secret_ref(_CONTRACT, KEY_ENV))
    except (SecretResolutionError, SecretStoreConfigurationError):
        raise SnapshotReadError(
            "Linear key is unavailable", code="key_unavailable"
        ) from None
    if key is None or not key.get_secret_value().strip():
        raise SnapshotReadError("Linear key is unavailable", code="key_unavailable")
    return key


def http_post(
    query: str, variables: dict[str, Any], *, key: SecretStr
) -> dict[str, Any]:
    """The production transport: one GraphQL POST with the store-resolved key."""
    import httpx

    response = httpx.post(
        LINEAR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": key.get_secret_value()},
        timeout=HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise SnapshotReadError("Linear refused the GraphQL read")
    data = body.get("data")
    if not isinstance(data, dict):
        raise SnapshotReadError("Linear answered with no data")
    return data


def _issue(node: dict[str, Any], viewer: str) -> ModelSnapshotIssue:
    state = node.get("state") or {}
    assignee = node.get("assignee") or {}
    labels = ((node.get("labels") or {}).get("nodes")) or []
    priority = node.get("priorityLabel") if node.get("priority") else None
    return ModelSnapshotIssue(
        id=node.get("identifier") or "",
        title=node.get("title") or "",
        status=state.get("name") or "",
        status_type=state.get("type") or "",
        assignee=assignee.get("id") or viewer,
        labels=tuple(str(label["name"]) for label in labels if label.get("name")),
        project=(node.get("project") or {}).get("name") or "",
        updated_at=node.get("updatedAt") or "",
        description=node.get("description") or "",
        priority=priority,
    )


def read_snapshot(post: Post, page_size: int, max_pages: int) -> ModelLinearSnapshot:
    """All pages of the subject's open issues; a list the page limit would cut short is an error."""
    viewer = ""
    issues: list[ModelSnapshotIssue] = []
    cursor: str | None = None
    for _ in range(max_pages):
        data = post(QUERY, {"first": page_size, "after": cursor})
        viewer = str((data.get("viewer") or {}).get("id") or "")
        if not viewer:
            raise SnapshotReadError("Linear named no signed-in user")
        page = data.get("issues") or {}
        issues.extend(_issue(n, viewer) for n in page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return ModelLinearSnapshot(operator=viewer, issues=tuple(issues))
        cursor = info.get("endCursor")
    raise SnapshotReadError(
        f"the read still had a next page after {max_pages} pages of {page_size}",
        code="pagination_incomplete",
    )


def render(snapshot: ModelLinearSnapshot) -> str:
    """The file's text: the typed model with its camel-case keys."""
    return json.dumps(snapshot.model_dump(mode="json", by_alias=True), indent=1) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class HandlerLinearSnapshot:
    """Writes the snapshot; ``post`` is the injectable Linear transport."""

    def __init__(self, post: Post | None = None) -> None:
        self._post = post

    def handle(self, request: ModelLinearSnapshotRequest) -> ModelLinearSnapshotResult:
        date = request.date or datetime.now(UTC).strftime("%Y-%m-%d")
        path = Path(request.out_dir) / f"linear-{date}.json"
        try:
            post = self._post or partial(http_post, key=resolve_key())
            snapshot = read_snapshot(post, request.page_size, request.max_pages)
            _write_atomic(path, render(snapshot))
        except (
            Exception
        ) as exc:  # the transport's own types (httpx), the model's, the disk's
            return ModelLinearSnapshotResult(
                status="failed",
                path=str(path),
                issue_count=0,
                with_description=0,
                # A transport exception can contain headers or ticket text. Only
                # controlled diagnostics and type names cross the result boundary.
                reason=(
                    {
                        "pagination_incomplete": "Linear still had a next page at the page limit",
                        "key_unavailable": "LINEAR_API_KEY is unavailable in the secret store",
                    }.get(exc.code, "SnapshotReadError")
                    if isinstance(exc, SnapshotReadError)
                    else type(exc).__name__
                ),
            )
        return ModelLinearSnapshotResult(
            status="written",
            path=str(path),
            issue_count=len(snapshot.issues),
            with_description=sum(bool(i.description) for i in snapshot.issues),
        )
