# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The routes reading of the delegation tracker (OMN-23381): delegations per hour by route, and the local share.

``read_routes`` asks the lab's postgres, through the injected command runner, for the hour counts in one read only transaction
over ssh, and never raises: a read that fails is a file with ``ok`` false and the reason. ``routes_block`` turns the file into
the tracker's block; a file that is missing, unparsable, failed or older than ``routes_max_age_min`` has no rows and no share.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    CommandRunner,
    _age,
    _load_json,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_delegation_routes import (
    ModelRouteCounts,
    ModelRouteHour,
    ModelRouteReads,
    ModelRoutesBlock,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelDelegationRuntime,
)

# The first line of a complete answer, so an empty window (no delegations) is told from an answer that never came.
ROUTES_MARKER = "routes-read"
_HOUR_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def routes_script(rt: ModelDelegationRuntime) -> str:
    """The shell script run on the database host: one read only transaction in the postgres container of the compose project."""
    src = rt.routes
    if src is None:
        raise ValueError("the overlay declares no routes block")
    hour = "date_trunc('hour', \"timestamp\" AT TIME ZONE 'UTC')"
    local = f"starts_with(backend_id, '{src.local_prefix}')"
    glm = f"starts_with(backend_id, '{src.glm_prefix}')"
    sql = "\n".join(
        [
            "BEGIN READ ONLY;",
            f"SELECT '{ROUTES_MARKER}';",
            f"SELECT json_build_object('hour', to_char({hour}, '{_HOUR_FORMAT}'), "
            f"'local', count(*) FILTER (WHERE {local}), "
            f"'glm', count(*) FILTER (WHERE {glm}), "
            f"'other', count(*) FILTER (WHERE NOT {local} AND NOT {glm}), "
            "'total', count(*))::text "
            f"FROM {rt.routes_table} "
            f"WHERE \"timestamp\" >= date_trunc('hour', now()) - interval '{src.window_hours - 1} hours' "
            f"AND data_source = '{rt.routes_data_source}' "
            f"GROUP BY {hour} ORDER BY {hour};",
            "COMMIT;",
        ]
    )
    return "\n".join(
        [
            "set -eu",
            "DB=$(docker ps --filter "
            f"label=com.docker.compose.project={src.compose_project} --filter "
            f"label=com.docker.compose.service={src.service} "
            "--filter status=running --format '{{.Names}}' | head -1)",
            '[ -n "$DB" ]',
            f"docker exec -i \"$DB\" psql -X -q -v ON_ERROR_STOP=1 -U {src.db_user} -d {src.database} -tA <<'SQL'",
            sql,
            "SQL",
        ]
    )


def parse_hours(stdout: str) -> tuple[tuple[ModelRouteCounts, ...], str]:
    """The hour rows of the answer, or why they were not usable. An empty answer is a quiet window only when the read said so."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines or lines[0].strip() != ROUTES_MARKER:
        return (), "unparsable: the answer does not begin with the read marker"
    rows: list[ModelRouteCounts] = []
    for line in lines[1:]:
        try:
            rows.append(ModelRouteCounts.model_validate(json.loads(line)))
        except (ValueError, ValidationError) as exc:
            return (), f"unparsable row {line.strip()[:80]!r}: {str(exc)[:120]}"
    return tuple(rows), ""


def read_routes(
    rt: ModelDelegationRuntime,
    now: datetime,
    run: CommandRunner,
    environ: Mapping[str, str],
) -> ModelRouteReads:
    """Read the hour counts once. Never raises; a failure is returned as ``ok`` false with its reason."""
    at = _stamp(now)
    src = rt.routes
    if src is None:
        return ModelRouteReads(
            read_at=at,
            ok=False,
            reason="the overlay declares no routes block",
            window_hours=1,
        )

    def failed(reason: str) -> ModelRouteReads:
        return ModelRouteReads(
            read_at=at, ok=False, reason=reason[:300], window_hours=src.window_hours
        )

    host = environ.get(src.ssh_host_env, "").strip()
    if not host:
        return failed(f"ssh target variable {src.ssh_host_env} is unset")
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "--",
        host,
        f"bash -c {shlex.quote(routes_script(rt))}",
    ]
    code, out, err = run(argv, rt.routes_read_timeout_seconds)
    if code != 0:
        tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
        return failed(f"exit {code}: {tail[0][:200]}")
    hours, problem = parse_hours(out)
    if problem:
        return failed(problem)
    return ModelRouteReads(
        read_at=at, ok=True, window_hours=src.window_hours, hours=hours
    )


def load_route_reads(path: Path) -> tuple[ModelRouteReads | None, str]:
    """The routes file, or None and the reason it cannot be used."""
    doc, problem = _load_json(path)
    if problem:
        return None, problem
    try:
        return ModelRouteReads.model_validate(doc), ""
    except ValueError as exc:
        return None, f"{path.name} is unreadable: {str(exc)[:160]}"


def _share(counts: ModelRouteCounts) -> ModelRouteHour:
    return ModelRouteHour(
        **counts.model_dump(),
        local_share=counts.local / counts.total if counts.total else None,
    )


def routes_block(
    rt: ModelDelegationRuntime,
    reads: ModelRouteReads | None,
    problem: str,
    now: datetime,
) -> ModelRoutesBlock:
    """The tracker's routes block. Only a fresh read has rows and a share."""
    limit = rt.routes_max_age_min

    def unread(reason: str, read_at: str = "") -> ModelRoutesBlock:
        return ModelRoutesBlock(
            state="unread", reason=reason, read_at=read_at, max_age_min=limit
        )

    if reads is None:
        return unread(problem or "the routes were not read")
    if not reads.ok:
        return unread(reads.reason or "the read failed", reads.read_at)
    age = _age(reads.read_at, now)
    if age is None:
        return unread(f"read_at {reads.read_at!r} is not a time")
    minutes = age // 60
    if age > limit * 60:
        return ModelRoutesBlock(
            state="stale",
            reason=f"the read is {minutes} min old, older than the {limit} min limit",
            read_at=reads.read_at,
            age_minutes=minutes,
            max_age_min=limit,
        )
    rows = tuple(_share(h) for h in reads.hours)
    read_hour = _stamp(
        datetime.fromisoformat(reads.read_at.replace("Z", "+00:00")).replace(
            minute=0, second=0, microsecond=0
        )
    )
    last = next((r for r in rows if r.hour == read_hour), None) or _share(
        ModelRouteCounts(hour=read_hour, local=0, glm=0, other=0, total=0)
    )
    return ModelRoutesBlock(
        state="fresh",
        read_at=reads.read_at,
        age_minutes=minutes,
        max_age_min=limit,
        window_hours=reads.window_hours,
        hours=rows,
        last_hour=last,
        local_share=last.local_share,
    )
