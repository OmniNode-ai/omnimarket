# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled read and the command route of the MVP launch block (OMN-23205, plan task M1).

``refresh`` is a step of the floor publish: it lists the scheduled runs of each producer workflow the block's overlay names
through the source-control node (``onex scm run-list``, one read at a time, the least recently read first, stopping at the stage
deadline) and keeps the answers in a record file. ``answer`` is the command the block's source port runs: one JSON request on
stdin, one JSON answer on stdout, made from that record and the launch candidate manifest file. A workflow that was not read, whose
read failed or whose read is older than the bound answers with its reason, so the block draws that reading NOT WORKING with the
reason and never reads an old record as current. Every path, bound and command is the node contract's.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.models.floor_17a.model_mvp_launch import (
    ModelMvpLaunchOverlay,
    ModelMvpRun,
)
from omnimarket.models.floor_17a.model_mvp_launch_reads import (
    ModelMvpReads,
    ModelMvpSourceRead,
)
from omnimarket.models.model_source_read_cache import (
    EnumSourceCacheOutcome,
    ModelSourceCacheKey,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.repo_revision import (
    cached_repo_head,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.schedule_revision import (
    SLOT_STAMP,
    schedule_slot,
)

# Runs a command and returns (exit code, stdout, stderr). The tests replace it.
CommandRunner = Callable[[Sequence[str], int], tuple[int, str, str]]


def _json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def read_key(repo: str, workflow: str) -> str:
    return f"{repo}:{workflow}"


def declared_sources(
    overlay: ModelMvpLaunchOverlay,
) -> dict[str, tuple[str, str, int, tuple[str, ...]]]:
    """Each producer workflow the overlay names once: key to (repo, workflow, run limit, the events that count as scheduled)."""
    found: dict[str, tuple[str, str, int, set[str]]] = {}
    for spec in (*overlay.readings, *overlay.scenarios):
        if spec.kind != "scheduled" or spec.source is None:
            continue
        key = read_key(spec.source.repo, spec.source.workflow)
        repo, workflow, limit, events = found.get(
            key, (spec.source.repo, spec.source.workflow, 0, set())
        )
        found[key] = (
            repo,
            workflow,
            max(limit, spec.source.limit),
            events | {"schedule", *spec.admitted_triggers},
        )
    return {k: (r, w, n, tuple(sorted(e))) for k, (r, w, n, e) in found.items()}


def declared_schedules(overlay: ModelMvpLaunchOverlay) -> dict[str, tuple[str, ...]]:
    """Each producer workflow's key to the distinct cron texts of the scheduled readings that name it."""
    found: dict[str, set[str]] = {}
    for spec in (*overlay.readings, *overlay.scenarios):
        if spec.kind != "scheduled" or spec.source is None or spec.cron is None:
            continue
        found.setdefault(read_key(spec.source.repo, spec.source.workflow), set()).add(
            spec.cron
        )
    return {k: tuple(sorted(v)) for k, v in found.items()}


def list_is_current(read: ModelMvpSourceRead, slot: datetime) -> bool:
    """True when the list needs no new read for ``slot``: no error, nothing still running, and the slot's run has appeared."""
    return (
        read.error is None
        and all(r.status == "completed" for r in read.runs)
        and any(r.created_at >= slot for r in read.runs)
    )


# The source-control node's typed answer for a workflow GitHub does not have: it stays so until a commit adds the file.
MISSING_WORKFLOW_MARK = "HTTP 404"


def is_missing_workflow(read: ModelMvpSourceRead) -> bool:
    return read.error is not None and MISSING_WORKFLOW_MARK in read.error


def _encode_read(read: ModelMvpSourceRead) -> str:
    return read.model_dump_json()


def _run_from(raw: Mapping[str, Any]) -> ModelMvpRun:
    conclusion = raw.get("conclusion")
    return ModelMvpRun(
        run_id=str(raw.get("id") or ""),
        created_at=datetime.fromisoformat(str(raw.get("created_at") or "")),
        event=str(raw.get("event") or ""),
        status=str(raw.get("status") or ""),
        conclusion=str(conclusion) if conclusion else None,
    )


def read_workflow(
    onex: Path,
    repo: str,
    workflow: str,
    limit: int,
    events: Sequence[str],
    timeout_seconds: int,
    run: CommandRunner,
    now: datetime,
) -> ModelMvpSourceRead:
    """The runs of one workflow for each counted event, or the reason the source-control node did not answer."""
    runs: dict[str, ModelMvpRun] = {}
    for event in events:
        code, out, err = run(
            [
                str(onex),
                "scm",
                "run-list",
                repo,
                "--workflow",
                workflow,
                "--event",
                event,
                "--limit",
                str(limit),
            ],
            timeout_seconds,
        )
        answer = _json_line(out)
        if answer is None:
            tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
            return ModelMvpSourceRead(
                repo=repo,
                workflow=workflow,
                read_at=now,
                error=f"run-list exit {code}: {tail[0][:200]}",
            )
        result = answer.get("result")
        if not isinstance(result, dict):
            return ModelMvpSourceRead(
                repo=repo,
                workflow=workflow,
                read_at=now,
                error="run-list answered no result",
            )
        if answer.get("outcome") != "completed":
            reason = str(
                result.get("failure_reason") or answer.get("outcome") or "failed"
            )
            return ModelMvpSourceRead(
                repo=repo, workflow=workflow, read_at=now, error=reason[:200]
            )
        listed = result.get("runs")
        if not isinstance(listed, list):
            return ModelMvpSourceRead(
                repo=repo,
                workflow=workflow,
                read_at=now,
                error="run-list answered no runs",
            )
        try:
            for item in listed:
                if isinstance(item, dict):
                    parsed = _run_from(item)
                    runs[parsed.run_id] = parsed
        except (ValueError, ValidationError):
            return ModelMvpSourceRead(
                repo=repo,
                workflow=workflow,
                read_at=now,
                error="run-list answered a malformed run",
            )
    return ModelMvpSourceRead(
        repo=repo, workflow=workflow, read_at=now, runs=tuple(runs.values())
    )


def load_reads(path: Path) -> ModelMvpReads | None:
    """The record as the last refresh left it, or ``None`` when there is no usable file."""
    try:
        return ModelMvpReads.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def refresh(
    overlay: ModelMvpLaunchOverlay,
    *,
    home: Path,
    onex_relpath: str,
    reads_relpath: str,
    read_timeout_seconds: int,
    stage_deadline_seconds: int,
    refresh_seconds: int,
    run: CommandRunner,
    clock: Callable[[], datetime],
    cache: CachedSourceReader | None = None,
    revision_run: CommandRunner | None = None,
) -> ModelMvpReads:
    """Re-read the stalest workflows inside the stage deadline, keep the others as they are, write the record and return it.

    A workflow read within ``refresh_seconds`` is not read again; one the deadline left no time for keeps its previous read, which
    the command route draws stale once it is older than its bound. A workflow the overlay no longer names is dropped. With a
    ``cache``, a workflow whose list the cache holds for the current schedule slot is not read from the source at all.
    """
    path = home / reads_relpath
    sources = declared_sources(overlay)
    schedules = declared_schedules(overlay)
    previous = load_reads(path)
    kept = {read_key(r.repo, r.workflow): r for r in previous.reads} if previous else {}
    result = {k: v for k, v in kept.items() if k in sources}
    started = clock()
    stalest_first = sorted(
        sources,
        key=lambda k: (
            result[k].read_at if k in result else datetime.min.replace(tzinfo=UTC)
        ),
    )
    for key in stalest_first:
        if (
            key in result
            and (started - result[key].read_at).total_seconds() < refresh_seconds
        ):
            continue
        elapsed = (clock() - started).total_seconds()
        if elapsed + read_timeout_seconds > stage_deadline_seconds:
            break
        repo, workflow, limit, events = sources[key]

        def fetch(
            repo: str = repo,
            workflow: str = workflow,
            limit: int = limit,
            events: tuple[str, ...] = events,
        ) -> ModelMvpSourceRead:
            return read_workflow(
                home / onex_relpath,
                repo,
                workflow,
                limit,
                events,
                read_timeout_seconds,
                run,
                clock(),
            )

        head = cached_repo_head(cache, home, repo, revision_run)
        absent_key = (
            ModelSourceCacheKey(
                source_id=f"mvp-runs-absent:{repo}:{workflow}",
                params_digest=ModelSourceCacheKey.digest({"workflow": workflow}),
                revision=head,
            )
            if cache is not None and head is not None
            else None
        )
        if cache is not None and absent_key is not None:
            remembered = cache.lookup(
                absent_key,
                ModelMvpSourceRead.model_validate_json,
                usable=is_missing_workflow,
            )
            if remembered is not None:
                result[key] = remembered.model_copy(update={"read_at": clock()})
                continue
        slot = schedule_slot(schedules.get(key, ()), clock())
        if cache is None or slot is None:
            result[key] = fetch()
            if (
                cache is not None
                and absent_key is not None
                and is_missing_workflow(result[key])
            ):
                cache.store(absent_key, result[key], _encode_read)
            continue

        def current(read: ModelMvpSourceRead, slot: datetime = slot) -> bool:
            return list_is_current(read, slot)

        listed, outcome = cache.read(
            ModelSourceCacheKey(
                source_id=f"mvp-runs:{repo}:{workflow}",
                params_digest=ModelSourceCacheKey.digest(
                    {"limit": limit, "events": list(events)}
                ),
                revision=slot.strftime(SLOT_STAMP),
            ),
            fetch,
            _encode_read,
            ModelMvpSourceRead.model_validate_json,
            usable=current,
            store_if=current,
        )
        # A hit is current by its key (the same slot, nothing running, the slot's run seen): it carries this cycle's read time.
        result[key] = (
            listed.model_copy(update={"read_at": clock()})
            if outcome == EnumSourceCacheOutcome.HIT
            else listed
        )
        if cache is not None and absent_key is not None and is_missing_workflow(listed):
            cache.store(absent_key, listed, _encode_read)
    record = ModelMvpReads(
        updated_at=clock(), reads=tuple(result[k] for k in sources if k in result)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(record.model_dump_json(indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return record


def read_candidate_manifest(path: Path) -> dict[str, object]:
    """The command route's answer to ``candidate``: the manifest's ``versions`` (name to version), or an ``error`` reason."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"error": "candidate-manifest-not-found"}
    except (OSError, ValueError):
        return {"error": "candidate-manifest-unreadable"}
    versions = doc.get("versions") if isinstance(doc, dict) else None
    if not isinstance(versions, dict) or not versions:
        return {"error": "candidate-manifest-malformed"}
    return {"versions": versions}


def answer(
    request: Mapping[str, Any],
    *,
    reads_path: Path,
    candidate_path: Path,
    reads_max_age_seconds: int,
    now: datetime,
) -> dict[str, object]:
    """One request of the block's source port, answered from the record. Never a guess: a reason when it cannot be."""
    query = request.get("query")
    if query == "candidate":
        return read_candidate_manifest(candidate_path)
    if query != "runs":
        return {"error": f"unknown-query:{query}"}
    record = load_reads(reads_path)
    if record is None:
        return {"error": "reads-record-missing"}
    wanted = read_key(str(request.get("repo", "")), str(request.get("workflow", "")))
    found = next(
        (r for r in record.reads if read_key(r.repo, r.workflow) == wanted), None
    )
    if found is None:
        return {"error": "workflow-not-read"}
    if (now - found.read_at).total_seconds() > reads_max_age_seconds:
        return {"error": "read-stale"}
    if found.error is not None:
        return {"error": found.error}
    allowed = request.get("triggers")
    runs = [
        r.model_dump(mode="json")
        for r in found.runs
        if not isinstance(allowed, list) or r.event in allowed
    ]
    return {"runs": runs}
