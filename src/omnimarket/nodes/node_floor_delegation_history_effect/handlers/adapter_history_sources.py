# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three readings of the delegation history: the lab projection, the routing overlay's backend settings and the experiment reports.

Each reader takes an injected command runner and never raises: a read that fails is a result with ``ok`` false and the reason.
The projection is read in one read only transaction over ssh, grouped by hour server side, so no prompt or response text and no
single run leaves the database; only counts, cost, the latency of each run and the first and last time a model was seen.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.models.delegation.model_delegation_routing_overlay import (
    ModelDelegationRoutingOverlay,
)
from omnimarket.nodes.node_floor_delegation_effect.handlers.adapter_delegation_sources import (
    CommandRunner,
)
from omnimarket.nodes.node_floor_delegation_history_compute.models.model_delegation_history import (
    ModelHistorySource,
)
from omnimarket.nodes.node_floor_delegation_history_compute.models.model_history_request import (
    ModelAttemptCounts,
    ModelBackendSettings,
    ModelLatencyRead,
    ModelSeenRead,
)
from omnimarket.nodes.node_floor_delegation_history_effect.handlers.adapter_report_tables import (
    ReportFile,
    split_remote_listing,
)
from omnimarket.nodes.node_floor_delegation_history_effect.models.model_floor_delegation_history import (
    ModelHistoryRuntime,
    ModelReportSource,
)

# The first line of a complete projection answer, so an empty window is told from an answer that never came.
PROJECTION_MARKER = "history-read"
_STAMP_FORMAT = 'YYYY-MM-DD"T"HH24:MI:SS"Z"'


class ModelProjectionRead(BaseModel):
    """The projection reading: the three row kinds, or the reason none could be made."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    reason: str = ""
    attempts: tuple[ModelAttemptCounts, ...] = ()
    latencies: tuple[ModelLatencyRead, ...] = ()
    seen: tuple[ModelSeenRead, ...] = ()
    skipped_unattributed: int = 0


def _ssh_argv(rt: ModelHistoryRuntime, target: str, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={rt.ssh_connect_timeout_seconds}",
        "--",
        target,
        remote_command,
    ]


def projection_script(rt: ModelHistoryRuntime) -> str:
    """The shell script run on the database host: one read only transaction in the postgres container of the compose project."""
    db = rt.database
    hours = rt.policy.window_days * 24
    stamp = f"to_char(date_trunc('hour', \"timestamp\" AT TIME ZONE 'UTC'), '{_STAMP_FORMAT}')"
    runs = (
        'SELECT "timestamp", task_type, model_name, backend_id, host, latency_ms, quality_gate_passed, '
        "terminal_ok, terminal_failure_cause, attempt_history "
        f"FROM {rt.projection_table} "
        f"WHERE data_source = '{rt.projection_data_source}' "
        f"AND \"timestamp\" >= date_trunc('hour', now()) - interval '{hours} hours' "
        'AND "timestamp" <= now()'
    )
    attempts = (
        "SELECT r.\"timestamp\" AS ts, r.task_type, a->>'model_id' AS model_name, "
        "coalesce(a->>'backend_id', '') AS backend_id, coalesce(a->>'host', '') AS host, "
        "a->>'acceptance_decision' AS decision, a->>'failure_class' AS failure_class, "
        "coalesce((a->>'cost_usd')::numeric, 0) AS cost "
        "FROM runs r, jsonb_array_elements(r.attempt_history) a "
        "WHERE jsonb_typeof(r.attempt_history) = 'array' AND jsonb_array_length(r.attempt_history) > 0 "
        "UNION ALL "
        "SELECT r.\"timestamp\", r.task_type, r.model_name, coalesce(r.backend_id, ''), coalesce(r.host, ''), "
        "CASE WHEN r.quality_gate_passed AND r.terminal_ok IS NOT FALSE THEN 'accept' ELSE 'reject' END, "
        "r.terminal_failure_cause, 0 "
        "FROM runs r "
        "WHERE NOT (jsonb_typeof(r.attempt_history) = 'array' AND jsonb_array_length(r.attempt_history) > 0)"
    )
    bucket_ts = stamp.replace('"timestamp"', "ts")
    sql = "\n".join(
        [
            "BEGIN READ ONLY;",
            f"SELECT '{PROJECTION_MARKER}';",
            f"WITH runs AS ({runs}), atts AS ({attempts}) "
            "SELECT 'A ' || row_to_json(t)::text FROM ("
            f"SELECT {bucket_ts} AS bucket, coalesce(model_name, '') AS model_name, backend_id, host, "
            "coalesce(task_type, '') AS task_type, count(*)::int AS attempts, "
            "(count(*) FILTER (WHERE decision = 'accept'))::int AS accepted, "
            "(count(*) FILTER (WHERE decision IS DISTINCT FROM 'accept' AND failure_class = 'timeout'))::int AS timeouts, "
            "coalesce(sum(cost), 0)::float8 AS cost_usd "
            "FROM atts GROUP BY 1, 2, 3, 4, 5) t;",
            f"WITH runs AS ({runs}) "
            "SELECT 'L ' || row_to_json(t)::text FROM ("
            f"SELECT {stamp} AS bucket, coalesce(model_name, '') AS model_name, coalesce(backend_id, '') AS backend_id, "
            "coalesce(host, '') AS host, coalesce(task_type, '') AS task_type, "
            "json_agg(latency_ms ORDER BY latency_ms) AS latencies_ms "
            "FROM runs WHERE latency_ms IS NOT NULL AND latency_ms >= 0 "
            "GROUP BY 1, 2, 3, 4, 5) t;",
            "SELECT 'S ' || row_to_json(t)::text FROM ("
            "SELECT coalesce(model_name, '') AS model_name, coalesce(backend_id, '') AS backend_id, coalesce(host, '') AS host, "
            f"to_char(min(\"timestamp\") AT TIME ZONE 'UTC', '{_STAMP_FORMAT}') AS first_seen, "
            f"to_char(max(\"timestamp\") AT TIME ZONE 'UTC', '{_STAMP_FORMAT}') AS last_seen "
            f"FROM {rt.projection_table} WHERE data_source = '{rt.projection_data_source}' "
            "GROUP BY 1, 2, 3) t;",
            "COMMIT;",
        ]
    )
    return "\n".join(
        [
            "set -eu",
            "DB=$(docker ps --filter "
            f"label=com.docker.compose.project={db.compose_project} --filter "
            f"label=com.docker.compose.service={db.service} "
            "--filter status=running --format '{{.Names}}' | head -1)",
            '[ -n "$DB" ]',
            f"docker exec -i \"$DB\" psql -X -q -v ON_ERROR_STOP=1 -U {db.db_user} -d {db.database} -tA <<'SQL'",
            sql,
            "SQL",
        ]
    )


def parse_projection(stdout: str) -> ModelProjectionRead:
    """The rows of the answer, or why they were not usable."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines or lines[0].strip() != PROJECTION_MARKER:
        return ModelProjectionRead(
            ok=False,
            reason="unparsable: the answer does not begin with the read marker",
        )
    attempts: list[ModelAttemptCounts] = []
    latencies: list[ModelLatencyRead] = []
    seen: list[ModelSeenRead] = []
    skipped = 0
    for line in lines[1:]:
        kind, _, body = line.partition(" ")
        try:
            doc = json.loads(body)
            if kind == "A":
                row_a = ModelAttemptCounts.model_validate(doc)
                if not row_a.model_name:
                    skipped += 1
                else:
                    attempts.append(row_a)
            elif kind == "L":
                row_l = ModelLatencyRead.model_validate(doc)
                if not row_l.model_name:
                    skipped += 1
                else:
                    latencies.append(row_l)
            elif kind == "S":
                row_s = ModelSeenRead.model_validate(doc)
                if not row_s.model_name:
                    skipped += 1
                else:
                    seen.append(row_s)
            else:
                raise ValueError("unknown row kind")
        except (ValueError, ValidationError) as exc:
            return ModelProjectionRead(
                ok=False,
                reason=f"unparsable row {line.strip()[:80]!r}: {str(exc)[:100]}",
            )
    return ModelProjectionRead(
        ok=True,
        attempts=tuple(attempts),
        latencies=tuple(latencies),
        seen=tuple(seen),
        skipped_unattributed=skipped,
    )


def read_projection(
    rt: ModelHistoryRuntime, run: CommandRunner, environ: Mapping[str, str]
) -> ModelProjectionRead:
    """Read the projection once. Never raises; a failure is returned as ``ok`` false with its reason."""
    host = environ.get(rt.database.ssh_host_env, "").strip()
    if not host:
        return ModelProjectionRead(
            ok=False, reason=f"ssh target variable {rt.database.ssh_host_env} is unset"
        )
    argv = _ssh_argv(rt, host, f"bash -c {shlex.quote(projection_script(rt))}")
    code, out, err = run(argv, rt.projection_read_timeout_seconds)
    if code != 0:
        tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
        return ModelProjectionRead(ok=False, reason=f"exit {code}: {tail[0][:200]}")
    return parse_projection(out)


def routing_settings(
    overlay: ModelDelegationRoutingOverlay,
) -> tuple[ModelBackendSettings, ...]:
    """The settings the routing overlay records of each backend: where it is served, its window and its price."""
    found: dict[str, ModelBackendSettings] = {}

    def host_of(url: str | None) -> str | None:
        return urlsplit(url).hostname if url else None

    if overlay.local_endpoints is not None:
        for backend in overlay.local_endpoints.backends:
            placement = backend.placement
            found[backend.backend_id] = ModelBackendSettings(
                backend_id=backend.backend_id,
                model_name=backend.model_name,
                host=host_of(backend.endpoint_url),
                context_tokens=placement.max_context_tokens if placement else None,
            )
    if overlay.eval_arms is not None:
        arms = {a.backend_id: a for a in overlay.eval_arms.arms}
        for eval_backend in overlay.eval_arms.backends:
            arm = arms.get(eval_backend.backend_id)
            earlier = found.get(eval_backend.backend_id)
            found[eval_backend.backend_id] = ModelBackendSettings(
                backend_id=eval_backend.backend_id,
                model_name=eval_backend.model_name,
                host=host_of(eval_backend.endpoint_url)
                or (earlier.host if earlier else None),
                context_tokens=(arm.context_length if arm else None)
                or (earlier.context_tokens if earlier else None),
                price_in_usd_per_mtok=float(arm.price_in_usd_per_mtok) if arm else None,
                price_out_usd_per_mtok=float(arm.price_out_usd_per_mtok)
                if arm
                else None,
            )
    return tuple(found[k] for k in sorted(found))


def report_script(source: ModelReportSource, globs: Sequence[str]) -> str:
    """The shell script that lists every report of one host: a ``@@FILE <name> <mtime>`` line and the file's text, block after block."""
    return (
        f"cd {source.root} && for f in {' '.join(globs)}; do "
        '[ -f "$f" ] || continue; '
        'printf \'@@FILE %s %s\\n\' "$f" "$(stat -c %Y "$f")"; '
        'cat "$f"; echo; done'
    )


def read_reports(
    rt: ModelHistoryRuntime, run: CommandRunner, source: ModelReportSource
) -> tuple[tuple[ReportFile, ...], ModelHistorySource]:
    """Read the reports of one host. Never raises; a failure is returned as a source that is not ok."""
    script = report_script(source, rt.reports.all_globs())
    if source.ssh_target is None:
        argv = ["sh", "-c", script]
    else:
        argv = _ssh_argv(rt, source.ssh_target, f"sh -c {shlex.quote(script)}")
    code, out, err = run(argv, rt.report_read_timeout_seconds)
    ident = f"reports:{source.id}"
    if code != 0:
        tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
        return (), ModelHistorySource(
            id=ident, ok=False, detail=f"exit {code}: {tail[0][:200]}"
        )
    files, problems = split_remote_listing(out)
    if not files:
        return (), ModelHistorySource(
            id=ident, ok=False, detail="no report file matched the overlay's patterns"
        )
    extra = f"; {len(problems)} header problems" if problems else ""
    return files, ModelHistorySource(
        id=ident, ok=True, detail=f"{len(files)} report files read{extra}"
    )
