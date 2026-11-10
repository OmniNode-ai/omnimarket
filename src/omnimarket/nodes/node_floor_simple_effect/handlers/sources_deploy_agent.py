# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy agent's own results as a lab source (OMN-22605, 17a).

The deploy agent answers ``/health`` with its most recent finished job and ``/job/<id>`` with that job's errors. The floor
keeps what it saw of those answers in a bounded record of its own and reads the record: the last success, the last failure
with its reason, and how many finished jobs in a row failed. A merge that is not yet running reads as deploy lag; a deploy
that has failed every time since is a different fact, and this is the reading of it.

A job the agent ends as ``superseded`` was replaced by a newer build before it ran: it is not a failure and not a
success, so it is neither recorded nor counted (on 2026-11-06 the agent ended four jobs as superseded in one second
after a success, and each would have read as a failed deploy). An agent that cannot be read leaves the record as it was and marks it unreadable, so the source reads unread and never
reads quiet. The count of failures in a row is counted from what the floor recorded: a record that begins at the first
read cannot count the jobs that failed before it.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.models.floor_17a import (
    ModelLabSourceDef,
    ModelLabSourceRead,
    ModelLabStateOverlay,
    ModelLabSubject,
    ModelNodeEmission,
)

# The agent's terminal status for a job a newer build replaced before it ran: no verdict on the deploy path itself.
SUPERSEDED = "superseded"

DEPLOY_AGENT_EMITTER = "node_deploy_agent_read:floor_read"

Fetch = Callable[[str], Mapping[str, Any] | None]


def _stamp(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def http_fetch(timeout_s: int) -> Fetch:
    """GET a JSON document; ``None`` when the agent does not answer or answers something that is not a JSON object."""

    def fetch(url: str) -> Mapping[str, Any] | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as response:
                body = json.loads(response.read())
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    return fetch


def _source(overlay: ModelLabStateOverlay) -> ModelLabSourceDef:
    return next(s for s in overlay.sources if s.kind == "deploy_agent")


def _reason(job: Mapping[str, Any]) -> str:
    errors = job.get("errors")
    if isinstance(errors, list) and errors:
        return str(errors[0])[:160]
    phases = job.get("phase_results")
    if isinstance(phases, Mapping):
        broken = [
            str(k) for k, v in phases.items() if str(v) not in ("success", "skipped")
        ]
        if broken:
            return "phase not successful: " + ", ".join(broken)
    return "no reason in the agent's answer"


def record_deploy_agent(
    path: Path, overlay: ModelLabStateOverlay, fetch: Fetch, now: datetime
) -> None:
    """Read the agent once and fold what it says into the record at ``path``. Never raises for an unreadable agent."""
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
        # Entries the agent ended superseded are not results (OMN-22737). A record
        # written before that change may hold them; they leave here, before the
        # history bound is applied, so they cannot push a real result out of it.
        jobs: list[dict[str, Any]] = [
            j for j in previous.get("jobs", []) if j.get("status") != SUPERSEDED
        ]
    except (OSError, ValueError, AttributeError):
        jobs = []
    base = overlay.deploy_agent_url.rstrip("/")
    health = fetch(f"{base}/health") if base else None
    document: dict[str, Any]
    if health is None:
        document = {
            "observed_at": _stamp(now),
            "agent_readable": False,
            "error": "the deploy agent did not answer /health"
            if base
            else "the overlay names no deploy agent address",
            "state": "",
            "jobs": jobs,
        }
    else:
        last = health.get("last_result")
        if isinstance(last, Mapping) and last.get("correlation_id"):
            cid = str(last["correlation_id"])
            if last.get("status") != SUPERSEDED and all(
                j.get("correlation_id") != cid for j in jobs
            ):
                reason = ""
                if last.get("status") != "success":
                    detail = fetch(f"{base}/job/{cid}")
                    reason = _reason(detail if detail is not None else last)
                jobs.append(
                    {
                        "correlation_id": cid,
                        "status": str(last.get("status")),
                        "completed_at": last.get("completed_at"),
                        "reason": reason,
                    }
                )
        jobs = jobs[-overlay.deploy_agent_history :]
        document = {
            "observed_at": _stamp(now),
            "agent_readable": True,
            "error": "",
            "state": str(health.get("state", "")),
            "jobs": jobs,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=1), encoding="utf-8")
    temporary.replace(path)


def read_deploy_agent(
    path: Path, overlay: ModelLabStateOverlay, now: datetime
) -> ModelLabSourceRead:
    """Red when ``limit`` finished jobs in a row failed; unread when the agent cannot be read or the record is missing."""
    source = _source(overlay)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        observed = parse_stamp(record["observed_at"])
        readable = bool(record["agent_readable"])
        jobs = [j for j in record["jobs"] if j.get("status") != SUPERSEDED]
    except OSError as exc:
        return ModelLabSourceRead(
            id=source.id,
            status="unread",
            reason=f"the deploy agent record cannot be opened ({type(exc).__name__})",
        )
    except (ValueError, KeyError, TypeError):
        return ModelLabSourceRead(
            id=source.id,
            status="unread",
            reason="the deploy agent record cannot be parsed",
        )
    if not readable:
        return ModelLabSourceRead(
            id=source.id, status="unread", reason=str(record.get("error", "unreadable"))
        )
    limit = int(source.limit) if source.limit is not None else 1
    in_a_row = 0
    for job in reversed(jobs):
        if job.get("status") == "success":
            break
        in_a_row += 1
    succeeded = [j for j in jobs if j.get("status") == "success"]
    failed = [j for j in jobs if j.get("status") != "success"]
    last_ok = succeeded[-1]["completed_at"] if succeeded else None
    last_bad = failed[-1] if failed else None
    note = (
        f"last success {last_ok}"
        if last_ok
        else f"no success among the newest {len(jobs)} results (the record keeps only the newest {overlay.deploy_agent_history}; an older success is not shown)"
    )
    if last_bad is not None:
        note += (
            f"; last failure {last_bad.get('completed_at')}: {last_bad.get('reason')}"
        )
    subject = ModelLabSubject(
        name="dev deploy agent",
        ok=in_a_row < limit,
        value=float(in_a_row),
        note=f"{in_a_row} finished jobs in a row failed (red at {limit}); {note}"[:300],
    )
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=observed,
        summary=(
            f"{in_a_row} failed in a row, {len(failed)} of {len(jobs)} recorded results failed"
            if jobs
            else "no finished job recorded yet"
        ),
        subjects=(subject,),
    )


def deploy_agent_events(
    read: ModelLabSourceRead, piece: int, now: datetime, correlation_id: str
) -> tuple[ModelNodeEmission, ...]:
    """The row's golden or typed error chain for one cycle, for the floor's one emission sink."""
    start = ModelNodeEmission(
        node=DEPLOY_AGENT_EMITTER,
        piece=piece,
        correlation_id=correlation_id,
        phase="start",
        emitted_at=now,
    )
    if read.status != "read":
        code, detail = "deploy_agent_unread", read.reason
    elif any(not s.ok for s in read.subjects):
        code = "deploy_agent_failing"
        detail = "; ".join(s.note for s in read.subjects if not s.ok)
    else:
        return (
            start,
            start.model_copy(
                update={"phase": "success", "counters": {"deploy_agent_ok": 1}}
            ),
        )
    return (
        start,
        start.model_copy(
            update={
                "phase": "failure",
                "failure_code": code,
                "failure_detail": detail[:300],
            }
        ),
    )
