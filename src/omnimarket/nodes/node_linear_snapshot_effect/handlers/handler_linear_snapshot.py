# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Write ``linear-<UTC date>.json``: the signed-in subject's open Linear issues, typed once.

The lab fill generator reads the file for the operator's open tickets and plans-today reads it as the
identity sidecar. Until now only the compact ``.txt`` was written by code; the JSON was a prose step an
agent could skip, and with it absent the tick's ``lab-fill-generator`` line read ``linear_snapshot
unread`` and no ticket was generated. The read uses the ``LINEAR_API_KEY`` of the environment (never
printed) through Linear's GraphQL endpoint, pages with the cursor until ``hasNextPage`` is false and
refuses a page limit that would cut the list short. The result carries counts only.

Linear refuses reads with HTTP 429 when the account is over its rate limit. The read then waits the
``Retry-After`` the answer names (an exponential wait when it names none) up to a budget, reads only
what changed since the last good read (issues updated since then, by ``updatedAt``) and rebuilds the
whole list once a day. A read the budget cannot finish keeps the last good snapshot, copies it to
today's name when today has none, and records the snapshot's age in ``linear-snapshot-state.json``
beside it: ``stale`` (exit 0) while that age is under the limit, ``failed`` (exit 1) past it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.config.service_endpoints import LINEAR_GRAPHQL_URL
from omnimarket.nodes.node_linear_snapshot_effect.models.model_linear_snapshot import (
    ModelLinearSnapshot,
    ModelLinearSnapshotRequest,
    ModelLinearSnapshotResult,
    ModelLinearSnapshotState,
    ModelSnapshotIssue,
)

KEY_ENV = "LINEAR_API_KEY"
HTTP_TIMEOUT_S = 60.0
STATE_FILE = "linear-snapshot-state.json"
SNAPSHOT_FILE = re.compile(r"^linear-(\d{4}-\d{2}-\d{2})\.json$")
CLOSED_STATE_TYPES = frozenset({"completed", "canceled"})
#: The wait after a 429 that names no Retry-After: 30 s, doubling per refusal of one call, capped at 5 minutes.
BACKOFF_BASE_S = 30.0
BACKOFF_CAP_S = 300.0

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

#: The issues of the team updated after ``$since``, archived ones included, whoever they are assigned to and
#: whatever their state: the read of what changed, which also names what left the subject's open list.
QUERY_CHANGES = """
query SnapshotChanges($first: Int!, $after: String, $since: DateTimeOrDuration!) {
  viewer { id }
  issues(
    first: $first
    after: $after
    includeArchived: true
    filter: {
      team: { key: { eq: "OMN" } }
      updatedAt: { gt: $since }
    }
  ) {
    pageInfo { hasNextPage endCursor }
    nodes {
      identifier
      title
      description
      updatedAt
      archivedAt
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


class SnapshotRateLimitedError(SnapshotReadError):
    """Linear refused the read for rate (HTTP 429 or a RATELIMITED error); ``retry_after_s`` is its Retry-After."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


def retry_after_seconds(value: str | None, now: datetime) -> float | None:
    """A Retry-After header as seconds: delta-seconds or an HTTP date; ``None`` when absent or unreadable."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        return max(0.0, (parsedate_to_datetime(text) - now).total_seconds())
    except (TypeError, ValueError):
        return None


def _ratelimited(errors: object) -> bool:
    return isinstance(errors, list) and any(
        isinstance(e, dict) and (e.get("extensions") or {}).get("code") == "RATELIMITED"
        for e in errors
    )


def http_post(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """The production transport: one GraphQL POST with the environment's key."""
    import httpx

    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise SnapshotReadError(f"{KEY_ENV} is not set")
    response = httpx.post(
        LINEAR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers={"Authorization": key},
        timeout=HTTP_TIMEOUT_S,
    )
    retry_after = retry_after_seconds(
        response.headers.get("Retry-After"), datetime.now(UTC)
    )
    if response.status_code == 429:
        raise SnapshotRateLimitedError(
            "Linear answered 429 Too Many Requests", retry_after
        )
    try:
        body = response.json()
    except ValueError:
        body = {}
    if _ratelimited(body.get("errors")):
        raise SnapshotRateLimitedError(
            "Linear refused the read: RATELIMITED", retry_after
        )
    response.raise_for_status()
    if body.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in body["errors"])
        raise SnapshotReadError(f"Linear refused the read: {messages[:200]}")
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
        f"the read still had a next page after {max_pages} pages of {page_size}"
    )


def with_backoff(post: Post, budget_s: float, sleep: Callable[[float], None]) -> Post:
    """``post`` that waits out Linear's rate limit: its Retry-After, else a doubling wait, within one shared budget.

    A wait the rest of the budget cannot cover is not slept: the refusal is raised at once, so a run does
    not hold the hour for a limit that outlasts it.
    """
    waited = 0.0

    def call(query: str, variables: dict[str, Any]) -> dict[str, Any]:
        nonlocal waited
        refusals = 0
        while True:
            try:
                return post(query, variables)
            except SnapshotRateLimitedError as refused:
                wait = (
                    refused.retry_after_s
                    if refused.retry_after_s is not None
                    else min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**refusals)
                )
                wait = max(1.0, wait)
                if waited + wait > budget_s:
                    raise SnapshotRateLimitedError(
                        f"{refused} (a wait of {wait:.0f}s does not fit the {budget_s:.0f}s budget, {waited:.0f}s used)",
                        refused.retry_after_s,
                    ) from refused
                sleep(wait)
                waited += wait
                refusals += 1

    return call


def read_changes(
    post: Post, since: str, page_size: int, max_pages: int
) -> tuple[str, list[dict[str, Any]]]:
    """The signed-in user's id and every team issue updated after ``since``, all pages."""
    viewer = ""
    nodes: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        data = post(
            QUERY_CHANGES, {"first": page_size, "after": cursor, "since": since}
        )
        viewer = str((data.get("viewer") or {}).get("id") or "")
        if not viewer:
            raise SnapshotReadError("Linear named no signed-in user")
        page = data.get("issues") or {}
        nodes.extend(page.get("nodes") or [])
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return viewer, nodes
        cursor = info.get("endCursor")
    raise SnapshotReadError(
        f"the changes read still had a next page after {max_pages} pages of {page_size}"
    )


def _still_open_and_mine(node: dict[str, Any], viewer: str) -> bool:
    return (
        not node.get("archivedAt")
        and (node.get("assignee") or {}).get("id") == viewer
        and (node.get("state") or {}).get("type") not in CLOSED_STATE_TYPES
    )


def merge_changes(
    base: ModelLinearSnapshot, viewer: str, nodes: list[dict[str, Any]]
) -> ModelLinearSnapshot:
    """The base list with each changed issue replaced, or dropped when it left the subject's open list."""
    by_id = {issue.id: issue for issue in base.issues}
    for node in nodes:
        ident = node.get("identifier") or ""
        if _still_open_and_mine(node, viewer):
            by_id[ident] = _issue(node, viewer)
        else:
            by_id.pop(ident, None)
    ordered = sorted(by_id.values(), key=lambda i: (i.updated_at, i.id), reverse=True)
    return ModelLinearSnapshot(operator=viewer, issues=tuple(ordered))


def render(snapshot: ModelLinearSnapshot) -> str:
    """The file's text: the typed model with its camel-case keys."""
    return json.dumps(snapshot.model_dump(mode="json", by_alias=True), indent=1) + "\n"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _load_state(out_dir: Path) -> ModelLinearSnapshotState | None:
    try:
        return ModelLinearSnapshotState.model_validate_json(
            (out_dir / STATE_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return None


def _load_base(out_dir: Path, date: str) -> tuple[Path, ModelLinearSnapshot] | None:
    """The newest snapshot file dated ``date`` or earlier, when it reads back as the typed model."""
    try:
        names = sorted(
            (m.group(1), m.group(0))
            for m in (SNAPSHOT_FILE.match(n.name) for n in out_dir.iterdir())
            if m
        )
    except OSError:
        return None
    dated = [name for day, name in names if day <= date]
    if not dated:
        return None
    path = out_dir / dated[-1]
    try:
        return path, ModelLinearSnapshot.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return None


class HandlerLinearSnapshot:
    """Writes the snapshot; ``post`` is the injectable Linear transport, ``sleep`` and ``now`` the clock."""

    def __init__(
        self,
        post: Post | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._post = post or http_post
        self._sleep = sleep
        self._now = now

    def _record(
        self,
        out_dir: Path,
        state: ModelLinearSnapshotState | None,
        started: datetime,
        unrecorded: datetime,
        status: str,
        reason: str,
        *,
        good_at: datetime | None = None,
        full_at: datetime | None = None,
    ) -> int:
        """Write the sidecar; returns the snapshot's age in seconds. A sidecar that cannot be written is not the read's failure."""
        good = good_at or (_parse_iso(state.last_good_read_at) if state else unrecorded)
        full = full_at or (_parse_iso(state.last_full_read_at) if state else unrecorded)
        age = max(0, int((started - good).total_seconds()))
        sidecar = ModelLinearSnapshotState.model_validate(
            {
                "last_good_read_at": _iso(good),
                "last_full_read_at": _iso(full),
                "last_attempt_at": _iso(started),
                "last_attempt_status": status,
                "last_attempt_reason": reason,
                "age_s": age,
            }
        )
        with contextlib.suppress(OSError):
            _write_atomic(
                out_dir / STATE_FILE,
                json.dumps(sidecar.model_dump(mode="json", by_alias=True), indent=1)
                + "\n",
            )
        return age

    def handle(self, request: ModelLinearSnapshotRequest) -> ModelLinearSnapshotResult:
        started = self._now()
        date = request.date or started.strftime("%Y-%m-%d")
        out_dir = Path(request.out_dir)
        path = out_dir / f"linear-{date}.json"
        state = _load_state(out_dir)
        base = _load_base(out_dir, date)
        # With no sidecar the last good read is unrecorded: a snapshot file's own mtime is the best age there is.
        unrecorded = (
            datetime.fromtimestamp(base[0].stat().st_mtime, UTC)
            if base is not None
            else started
        )
        post = with_backoff(self._post, request.backoff_budget_s, self._sleep)
        mode = "full"
        try:
            snapshot: ModelLinearSnapshot | None = None
            if base is not None and state is not None:
                full_age = (
                    started - _parse_iso(state.last_full_read_at)
                ).total_seconds()
                if 0 <= full_age < request.full_read_every_s:
                    since = _parse_iso(state.last_good_read_at) - timedelta(
                        seconds=request.overlap_s
                    )
                    viewer, nodes = read_changes(
                        post, _iso(since), request.page_size, request.max_pages
                    )
                    if viewer == base[1].operator:
                        snapshot = merge_changes(base[1], viewer, nodes)
                        mode = "incremental"
            if snapshot is None:
                snapshot = read_snapshot(post, request.page_size, request.max_pages)
            _write_atomic(path, render(snapshot))
        except SnapshotRateLimitedError as refused:
            return self._keep_last_good(
                request, path, base, state, started, unrecorded, refused
            )
        except (
            Exception
        ) as exc:  # the transport's own types (httpx), the model's, the disk's
            reason = " ".join(f"{type(exc).__name__}: {exc}".split())[:300]
            self._record(out_dir, state, started, unrecorded, "failed", reason)
            return ModelLinearSnapshotResult(
                status="failed",
                path=str(path),
                issue_count=0,
                with_description=0,
                reason=reason,
            )
        self._record(
            out_dir,
            state,
            started,
            unrecorded,
            "written",
            "",
            good_at=started,
            full_at=started if mode == "full" else None,
        )
        return ModelLinearSnapshotResult(
            status="written",
            path=str(path),
            issue_count=len(snapshot.issues),
            with_description=sum(bool(i.description) for i in snapshot.issues),
            mode=mode,
            age_s=0,
        )

    def _keep_last_good(
        self,
        request: ModelLinearSnapshotRequest,
        path: Path,
        base: tuple[Path, ModelLinearSnapshot] | None,
        state: ModelLinearSnapshotState | None,
        started: datetime,
        unrecorded: datetime,
        refused: SnapshotRateLimitedError,
    ) -> ModelLinearSnapshotResult:
        """Linear's limit outlasted the budget: serve the last good snapshot, with its age, or fail when there is none or it is too old."""
        out_dir = Path(request.out_dir)
        reason = " ".join(f"SnapshotRateLimitedError: {refused}".split())[:300]
        if base is None:
            self._record(out_dir, state, started, unrecorded, "failed", reason)
            return ModelLinearSnapshotResult(
                status="failed",
                path=str(path),
                issue_count=0,
                with_description=0,
                reason=f"{reason}; no earlier snapshot to keep"[:300],
            )
        good = _parse_iso(state.last_good_read_at) if state else unrecorded
        age = max(0, int((started - good).total_seconds()))
        if age > request.max_stale_s:
            self._record(
                out_dir, state, started, unrecorded, "failed", reason, good_at=good
            )
            return ModelLinearSnapshotResult(
                status="failed",
                path=str(path),
                issue_count=len(base[1].issues),
                with_description=sum(bool(i.description) for i in base[1].issues),
                reason=f"{reason}; the kept snapshot is {age}s old, over the {request.max_stale_s}s limit"[
                    :300
                ],
                age_s=age,
            )
        if not path.exists():
            _write_atomic(path, render(base[1]))
        self._record(out_dir, state, started, unrecorded, "stale", reason, good_at=good)
        return ModelLinearSnapshotResult(
            status="stale",
            path=str(path),
            issue_count=len(base[1].issues),
            with_description=sum(bool(i.description) for i in base[1].issues),
            reason=f"{reason}; serving the snapshot read {age}s ago"[:300],
            age_s=age,
        )
