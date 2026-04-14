# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPipelineFill — pipeline fill orchestrator.

Orchestrates one fill cycle:
  1. Query Linear for unstarted active-sprint tickets
  2. Filter already-dispatched / blocked tickets
  3. Score via HandlerRsdFill (node_rsd_fill_compute)
  4. Enforce wave-cap and min-score
  5. Publish onex.cmd.omnimarket.ticket-pipeline-start.v1 for each top-N ticket
  6. Write durable state to .onex_state/pipeline-fill/dispatched.yaml

Designed for headless/cron invocation via onex.cmd.omnimarket.pipeline-fill-start.v1.

Related:
    - OMN-8688: node_pipeline_fill
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml

from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_command import (
    ModelPipelineFillCommand,
)
from omnimarket.nodes.node_pipeline_fill.models.model_pipeline_fill_result import (
    ModelPipelineFillResult,
)
from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_rsd_fill_compute.models.model_scored_ticket import (
    ModelScoredTicket,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["ORCHESTRATOR"]

# Linear project ID for the active sprint backlog.
# Linear rejects name-based project filters (400); must use project ID.
# Override via LINEAR_ACTIVE_SPRINT_PROJECT_ID env var.
_ACTIVE_SPRINT_PROJECT_ID = os.environ.get(
    "LINEAR_ACTIVE_SPRINT_PROJECT_ID", "1af15047-d06a-4ffc-855d-da70ff124dba"
)
_UNSTARTED_STATES = frozenset({"Backlog", "Todo"})

HANDLER_TYPE: HandlerType = "NODE_HANDLER"
HANDLER_CATEGORY: HandlerCategory = "ORCHESTRATOR"

# Topics read from contract — never hardcode in application logic
_TOPIC_TICKET_PIPELINE_START = "onex.cmd.omnimarket.ticket-pipeline-start.v1"
_TOPIC_PIPELINE_FILL_COMPLETED = "onex.evt.omnimarket.pipeline-fill-completed.v1"


_LINEAR_API_URL = "https://api.linear.app/graphql"

_QUERY_ACTIVE_SPRINT_UNSTARTED = """
query ActiveSprintUnstarted($projectId: ID!, $first: Int!) {
  issues(
    filter: {
      project: { id: { eq: $projectId } }
      state: { type: { in: ["backlog", "unstarted"] } }
    }
    first: $first
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      priority
      description
      createdAt
      state { name }
      labels { nodes { name } }
      relations { nodes { type relatedIssue { state { name } } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


class LinearHttpClient:
    """Default Linear client: queries the active sprint project via the GraphQL API.

    Reads LINEAR_API_KEY from the environment. In tests, patch `_list_issues`
    to inject fixture data without hitting Linear.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("LINEAR_API_KEY", "")

    async def _list_issues(self, project_id: str, limit: int = 250) -> dict[str, Any]:
        """Execute the GraphQL query against the Linear API."""
        import urllib.request

        if not self._api_key:
            logger.warning(
                "pipeline_fill: LINEAR_API_KEY not set — returning empty result"
            )
            return {"issues": []}

        payload = {
            "query": _QUERY_ACTIVE_SPRINT_UNSTARTED,
            "variables": {"projectId": project_id, "first": limit},
        }
        body = __import__("json").dumps(payload).encode()
        req = urllib.request.Request(
            _LINEAR_API_URL,
            data=body,
            headers={
                "Authorization": self._api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        loop = __import__("asyncio").get_event_loop()
        import functools

        def _sync_request() -> dict[str, Any]:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return __import__("json").loads(resp.read())  # type: ignore[no-any-return]

        data: dict[str, Any] = await loop.run_in_executor(
            None, functools.partial(_sync_request)
        )
        nodes = data.get("data", {}).get("issues", {}).get("nodes", [])
        return {"issues": nodes}

    async def list_active_sprint_unstarted(self) -> list[dict[str, Any]]:
        """Return all unstarted (Backlog/unstarted state) tickets from the active sprint project."""
        page = await self._list_issues(project_id=_ACTIVE_SPRINT_PROJECT_ID)
        all_issues: list[dict[str, Any]] = []
        for issue in page.get("issues", []):
            state_name = (
                issue.get("state", {}).get("name", "")
                if isinstance(issue.get("state"), dict)
                else ""
            )
            if state_name in _UNSTARTED_STATES:
                all_issues.append(issue)
        return all_issues


def _load_contract() -> dict[str, Any]:
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with open(contract_path) as f:
        result: dict[str, Any] = yaml.safe_load(f)
        return result


def _resolve_omni_home() -> Path:
    omni_home = os.environ.get("OMNI_HOME", "")
    if omni_home:
        return Path(omni_home)
    # fallback: resolve from package location (worktrees or canonical clone)
    return Path(__file__).parents[8]


class HandlerPipelineFill:
    """Orchestrates one RSD-driven pipeline fill cycle."""

    def __init__(
        self,
        rsd_handler: HandlerRsdFill | None = None,
        linear_client: Any | None = None,
        event_bus: Any | None = None,
    ) -> None:
        self._rsd = rsd_handler or HandlerRsdFill()
        self._linear: Any = (
            linear_client if linear_client is not None else LinearHttpClient()
        )
        self._event_bus = (
            event_bus  # injected in production; None => fire-and-forget log
        )

    @property
    def handler_type(self) -> HandlerType:
        return HANDLER_TYPE

    @property
    def handler_category(self) -> HandlerCategory:
        return HANDLER_CATEGORY

    async def handle(
        self,
        command: ModelPipelineFillCommand,
    ) -> ModelPipelineFillResult:
        """Execute one pipeline fill cycle."""
        logger.info(
            "pipeline_fill: starting cycle correlation_id=%s dry_run=%s top_n=%d",
            command.correlation_id,
            command.dry_run,
            command.top_n,
        )

        state_dir = _resolve_omni_home() / command.state_dir
        state_dir.mkdir(parents=True, exist_ok=True)
        dispatched_path = state_dir / "dispatched.yaml"

        # Load existing dispatch state
        dispatch_state = _load_dispatch_state(dispatched_path)
        in_flight_ids = frozenset(
            e["ticket_id"] for e in dispatch_state.get("in_flight", [])
        )

        # Wave-cap check
        in_flight_count = len(in_flight_ids)
        if in_flight_count >= command.wave_cap:
            logger.info(
                "pipeline_fill: wave cap reached (%d/%d), skipping cycle",
                in_flight_count,
                command.wave_cap,
            )
            skip_reason = f"Wave cap reached ({in_flight_count}/{command.wave_cap})"
            _write_last_run(
                state_dir / "last-run.yaml",
                correlation_id=command.correlation_id,
                candidates_found=0,
                candidates_after_filter=0,
                dispatched=[],
                wave_status=f"{in_flight_count}/{command.wave_cap} in-flight",
                dry_run=command.dry_run,
                skip_reason=skip_reason,
            )
            return ModelPipelineFillResult(
                correlation_id=command.correlation_id,
                candidates_found=0,
                candidates_after_filter=0,
                skip_reason=skip_reason,
                dry_run=command.dry_run,
            )

        # Fetch candidates from Linear
        raw_tickets = await self._fetch_active_sprint_tickets()
        candidates_found = len(raw_tickets)

        # Filter: exclude in-flight and blocked tickets
        filtered = [
            t
            for t in raw_tickets
            if t["id"] not in in_flight_ids and not _is_blocked(t)
        ]
        candidates_after_filter = len(filtered)

        if not filtered:
            logger.info("pipeline_fill: no dispatchable candidates after filtering")
            skip_reason = "No dispatchable candidates after filtering"
            _write_last_run(
                state_dir / "last-run.yaml",
                correlation_id=command.correlation_id,
                candidates_found=candidates_found,
                candidates_after_filter=0,
                dispatched=[],
                wave_status=f"{in_flight_count}/{command.wave_cap} in-flight",
                dry_run=command.dry_run,
                skip_reason=skip_reason,
            )
            return ModelPipelineFillResult(
                correlation_id=command.correlation_id,
                candidates_found=candidates_found,
                candidates_after_filter=0,
                skip_reason=skip_reason,
                dry_run=command.dry_run,
            )

        # Convert to ModelScoredTicket and score via HandlerRsdFill
        scored_input = tuple(_to_scored_ticket(t) for t in filtered)
        fill_result = await self._rsd.handle(
            correlation_id=command.correlation_id,
            scored_tickets=scored_input,
            max_tickets=command.top_n,
        )

        # Write scores.yaml — observable artifact for audit / debugging
        _write_scores(
            state_dir / "scores.yaml",
            correlation_id=command.correlation_id,
            scored_tickets=fill_result.selected_tickets,
        )

        # Apply min-score filter
        eligible = tuple(
            t for t in fill_result.selected_tickets if t.rsd_score >= command.min_score
        )

        if not eligible:
            logger.info(
                "pipeline_fill: no tickets above min_score=%.2f", command.min_score
            )
            skip_reason = f"No tickets above min_score={command.min_score}"
            _write_last_run(
                state_dir / "last-run.yaml",
                correlation_id=command.correlation_id,
                candidates_found=candidates_found,
                candidates_after_filter=candidates_after_filter,
                dispatched=[],
                wave_status=f"{in_flight_count}/{command.wave_cap} in-flight",
                dry_run=command.dry_run,
                skip_reason=skip_reason,
            )
            return ModelPipelineFillResult(
                correlation_id=command.correlation_id,
                candidates_found=candidates_found,
                candidates_after_filter=candidates_after_filter,
                skipped=tuple(t.ticket_id for t in fill_result.selected_tickets),
                skip_reason=skip_reason,
                dry_run=command.dry_run,
            )

        # Respect remaining wave-cap slots
        available_slots = command.wave_cap - in_flight_count
        to_dispatch = eligible[:available_slots]
        skipped = tuple(t.ticket_id for t in eligible[available_slots:])

        dispatched_ids: list[str] = []
        now_iso = datetime.now(UTC).isoformat()

        for ticket in to_dispatch:
            if command.dry_run:
                logger.info(
                    "pipeline_fill: [dry_run] would dispatch %s (score=%.3f)",
                    ticket.ticket_id,
                    ticket.rsd_score,
                )
                dispatched_ids.append(ticket.ticket_id)
                continue

            # Publish dispatch event
            await self._dispatch_ticket(ticket, command.correlation_id)

            # Record in state
            dispatch_state.setdefault("in_flight", []).append(
                {
                    "ticket_id": ticket.ticket_id,
                    "dispatched_at": now_iso,
                    "worker_type": "ticket_pipeline",
                    "status": "running",
                    "rsd_score": ticket.rsd_score,
                }
            )
            dispatched_ids.append(ticket.ticket_id)
            logger.info(
                "pipeline_fill: dispatched %s (score=%.3f)",
                ticket.ticket_id,
                ticket.rsd_score,
            )

        if not command.dry_run:
            _save_dispatch_state(dispatched_path, dispatch_state)

        # Write last-run state
        _write_last_run(
            state_dir / "last-run.yaml",
            correlation_id=command.correlation_id,
            candidates_found=candidates_found,
            candidates_after_filter=candidates_after_filter,
            dispatched=dispatched_ids,
            wave_status=f"{in_flight_count + len(dispatched_ids)}/{command.wave_cap} in-flight",
            dry_run=command.dry_run,
        )

        return ModelPipelineFillResult(
            correlation_id=command.correlation_id,
            candidates_found=candidates_found,
            candidates_after_filter=candidates_after_filter,
            dispatched=tuple(dispatched_ids),
            skipped=skipped,
            dry_run=command.dry_run,
        )

    async def _fetch_active_sprint_tickets(self) -> list[dict[str, Any]]:
        """Fetch unstarted active-sprint tickets from Linear.

        In production this calls the Linear MCP or HTTP API. In tests the
        linear_client is injected. Returns a list of raw ticket dicts with at
        least: id, title, priority, labels, description, state.
        """
        result: list[dict[str, Any]] = await self._linear.list_active_sprint_unstarted()
        return result

    async def _dispatch_ticket(
        self, ticket: ModelScoredTicket, correlation_id: UUID
    ) -> None:
        """Publish a ticket-pipeline-start command event."""
        payload: dict[str, Any] = {
            "ticket_id": ticket.ticket_id,
            "correlation_id": str(correlation_id),
            "triggered_by": "node_pipeline_fill",
        }

        if self._event_bus is not None:
            await self._event_bus.publish(
                topic=_TOPIC_TICKET_PIPELINE_START,
                payload=payload,
            )
        else:
            # Standalone mode: log intent only (full runtime wires the bus)
            logger.info(
                "pipeline_fill: [no-bus] would publish %s: %s",
                _TOPIC_TICKET_PIPELINE_START,
                payload,
            )


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _load_dispatch_state(path: Path) -> dict[str, Any]:
    if path.exists():
        with open(path) as f:
            return yaml.safe_load(f) or {}
    return {"in_flight": [], "completed": [], "failed": []}


def _save_dispatch_state(path: Path, state: dict[str, Any]) -> None:
    with open(path, "w") as f:
        yaml.dump(state, f, default_flow_style=False, sort_keys=False)


def _write_last_run(
    path: Path,
    *,
    correlation_id: UUID,
    candidates_found: int,
    candidates_after_filter: int,
    dispatched: list[str],
    wave_status: str,
    dry_run: bool,
    skip_reason: str = "",
) -> None:
    data: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": str(correlation_id),
        "candidates_found": candidates_found,
        "candidates_after_filter": candidates_after_filter,
        "dispatched": dispatched,
        "wave_status": wave_status,
        "dry_run": dry_run,
    }
    if skip_reason:
        data["skip_reason"] = skip_reason
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


def _write_scores(
    path: Path,
    *,
    correlation_id: UUID,
    scored_tickets: tuple[ModelScoredTicket, ...],
) -> None:
    data = {
        "timestamp": datetime.now(UTC).isoformat(),
        "correlation_id": str(correlation_id),
        "scores": [
            {
                "ticket_id": t.ticket_id,
                "title": t.title,
                "rsd_score": round(t.rsd_score, 4),
                "priority": t.priority,
            }
            for t in scored_tickets
        ],
    }
    with open(path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Ticket conversion helpers
# ---------------------------------------------------------------------------


def _to_scored_ticket(raw: dict[str, Any]) -> ModelScoredTicket:
    """Convert a raw Linear ticket dict to a ModelScoredTicket with RSD score."""
    priority_map = {
        0: 0,  # None
        1: 1,  # Urgent
        2: 2,  # High
        3: 3,  # Medium
        4: 4,  # Low
    }
    raw_priority = raw.get("priority", 0)
    priority = priority_map.get(raw_priority, 0)

    labels: list[str] = [
        lbl.get("name", "") for lbl in raw.get("labels", {}).get("nodes", [])
    ]

    rsd_score = _compute_rsd_score(raw, labels)

    return ModelScoredTicket(
        ticket_id=raw.get("identifier", raw.get("id", "")),
        title=raw.get("title", ""),
        rsd_score=rsd_score,
        priority=priority,
        labels=tuple(labels),
        description=raw.get("description", "") or "",
        state=raw.get("state", {}).get("name", "")
        if isinstance(raw.get("state"), dict)
        else "",
    )


def _compute_rsd_score(raw: dict[str, Any], labels: list[str]) -> float:
    """Compute RSD acceleration score from ticket fields.

    Weights match the pipeline_fill SKILL.md spec:
      blocking_score  0.30
      priority_score  0.25
      staleness_score 0.20
      repo_readiness  0.15  (default 0.5 — no live check here)
      size_score      0.10
    """
    # blocking_score: normalized by relation count (max 5)
    blocking_relations = raw.get("relations", {}).get("nodes", [])
    blocking_count = sum(1 for r in blocking_relations if r.get("type") == "blocks")
    blocking_score = min(blocking_count / 5.0, 1.0)

    # priority_score
    priority_map_score = {0: 0.1, 1: 1.0, 2: 0.75, 3: 0.5, 4: 0.25}
    priority_score = priority_map_score.get(raw.get("priority", 0), 0.1)

    # staleness_score: days since createdAt, capped at 14 days
    staleness_score = 0.5  # default if no date
    created_at_str = raw.get("createdAt", "")
    if created_at_str:
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_days = (datetime.now(UTC) - created_at).days
            staleness_score = min(max((age_days / 14.0) * 0.9 + 0.1, 0.1), 1.0)
        except ValueError:
            pass

    # repo_readiness: default 0.5 (live check not available without GitHub API)
    repo_readiness_score = 0.5

    # size_score from estimate or labels
    size_score = _estimate_size_score(raw, labels)

    return (
        0.30 * blocking_score
        + 0.25 * priority_score
        + 0.20 * staleness_score
        + 0.15 * repo_readiness_score
        + 0.10 * size_score
    )


def _estimate_size_score(raw: dict[str, Any], labels: list[str]) -> float:
    size_label_map = {"xs": 1.0, "s": 0.8, "m": 0.5, "l": 0.3, "xl": 0.1}
    for lbl in labels:
        normalized = lbl.lower().strip()
        if normalized in size_label_map:
            return size_label_map[normalized]
    estimate = raw.get("estimate")
    if estimate is not None:
        if estimate <= 1:
            return 1.0
        if estimate <= 2:
            return 0.8
        if estimate <= 3:
            return 0.5
        if estimate <= 5:
            return 0.3
        return 0.1
    return 0.5


def _is_blocked(raw: dict[str, Any]) -> bool:
    labels: list[str] = [
        lbl.get("name", "").lower() for lbl in raw.get("labels", {}).get("nodes", [])
    ]
    if "blocked" in labels:
        return True
    relations = raw.get("relations", {}).get("nodes", [])
    for rel in relations:
        if rel.get("type") == "blocked_by":
            related_state = rel.get("relatedIssue", {}).get("state", {}).get("name", "")
            if related_state not in {"Done", "Canceled"}:
                return True
    return False
