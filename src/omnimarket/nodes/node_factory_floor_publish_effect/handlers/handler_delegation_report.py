# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read real lab projection events once for the floor and the session tick.

Runs count once in each rung they touched; attempts retain every refusal and
failure. Rung totals therefore overlap. Probes never enter traffic counts.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_factory_floor_publish_effect.models.model_delegation_report import (
    ModelDelegationReadOverlay,
    ModelDelegationReportPolicy,
    ModelDelegationReportRequest,
    ModelDelegationReportResult,
)

FIX = "restore local endpoint bindings in the deployed delegation overlay; run an unpinned document delegation and read its projection attempts"

# One read-only transaction, the database clock, no row cap and no prompt or
# response text. The all-time count is a positive control for a quiet window.
READ_SQL = """
BEGIN READ ONLY;
SELECT jsonb_build_object(
 'now', now(), 'total_rows', (SELECT count(*) FROM delegation_events),
 'latest_local', (SELECT max(e.timestamp) FROM delegation_events e
   WHERE e.data_source = 'real' AND (e.cost_tier_name = 'local' OR EXISTS (
    SELECT 1 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.attempt_history) = 'array'
     THEN e.attempt_history ELSE '[]'::jsonb END) a
    WHERE a->>'tier' = 'local' OR a->>'backend_id' LIKE 'local-%'))),
 'rows', coalesce((SELECT jsonb_agg(jsonb_build_object(
   'correlation_id', e.correlation_id, 'timestamp', e.timestamp,
   'data_source', e.data_source, 'model_name', e.model_name,
   'backend_id', e.backend_id, 'host', e.host, 'cost_tier_name', e.cost_tier_name,
   'quality_gate_passed', e.quality_gate_passed, 'terminal_ok', e.terminal_ok,
   'operational_outcome', e.operational_outcome,
   'attempt_history', (SELECT coalesce(jsonb_agg(jsonb_build_object(
     'backend_id', a->>'backend_id', 'tier', a->>'tier',
     'model_id', a->>'model_id', 'host', a->>'host',
     'endpoint', a->>'endpoint', 'acceptance_decision', a->>'acceptance_decision',
     'quality_gate_passed', a->'quality_gate_passed', 'failure_class', a->>'failure_class'
   )), '[]'::jsonb) FROM jsonb_array_elements(
     CASE WHEN jsonb_typeof(e.attempt_history) = 'array'
     THEN e.attempt_history ELSE '[]'::jsonb END) a)
 )) FROM delegation_events e WHERE e.data_source = 'real'
 AND e.timestamp > now() - interval '24 hours' AND e.timestamp <= now()), '[]'::jsonb));
COMMIT;
"""

# Resolve the same merged contract as production. A real probe passes through
# the production call handler, including its model and response checks.
CONFIG_PY = """
import hashlib, json, sys, time, uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import yaml
from omnimarket.handlers.routing.delegation_backend_resolution import load_bifrost_backends, resolve_delegation_backend
from omnimarket.handlers.routing.routing_tiers_path import load_delegation_routing_overlay
from omnimarket.events.llm_delegation_call import ModelLlmDelegationCallRequest
from omnimarket.nodes.node_llm_delegation_call_effect import HandlerLlmDelegationCall
rows = []
backends = load_bifrost_backends()
for endpoint in json.loads(sys.argv[2]):
 backends.append(dict(endpoint, provider='local', diagnostic_only=True))
for b in backends:
 if b.get('provider') != 'local' or 'embedding' in b.get('capabilities', []):
  continue
 endpoint = b.get('endpoint_url')
 row = {k: b.get(k) for k in ('backend_id', 'model_name', 'endpoint_url')}
 row['host'] = urlsplit(endpoint).hostname if endpoint else 'not configured'
 row.update(diagnostic_only=b.get('diagnostic_only', False), source=b.get('source', 'deployed routing resolver'))
 row.update(probe='not-probed' if endpoint else 'unbound', checked_at=datetime.now(UTC).isoformat())
 if not endpoint:
  try:
   resolve_delegation_backend('document', backend_id=b['backend_id'])
  except Exception as exc:
   row['resolver_refusal'] = type(exc).__name__
 if endpoint and sys.argv[1] == 'probe':
  prompt = 'Reply with exactly: pong /no_think'
  ident = str(uuid.uuid4()); start = time.monotonic()
  try:
   result = HandlerLlmDelegationCall().handle(ModelLlmDelegationCallRequest(
    request_id=ident, correlation_id=ident, causation_id=ident,
    model_id=b['model_name'], endpoint_ref=endpoint, prompt=prompt,
    prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(), task_type='document',
    max_tokens=32, temperature=0, timeout_seconds=20, model_tier='local', provider='local',
    inline_reasoning_terminator=b.get('inline_reasoning_terminator'),
    provider_request_options=b.get('provider_request_options') or {}))
   data = result.model_dump(mode='json')
   row['probe'] = 'answered' if data.get('success') is True or data.get('response_text') else 'failed'
   row['failure_class'] = data.get('failure_class') or ('call_refused' if row['probe'] == 'failed' else None)
  except Exception as exc:
   row.update(probe='failed', failure_class=type(exc).__name__)
  row['wall_seconds'] = round(time.monotonic()-start, 3)
 rows.append(row)
chains = [c.model_dump(mode='json') for c in load_delegation_routing_overlay().escalation_chains]
print(json.dumps({'endpoints': rows, 'chains': chains}))
"""


Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


class PublishRefusedError(RuntimeError):
    """A source, setting or transfer is missing or failed; the message names it and the fix."""


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this node; tests replace it."""
    try:
        return subprocess.run(
            list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise PublishRefusedError(
            f"{argv[0]} did not finish within {timeout} s"
        ) from exc
    except OSError as exc:
        raise PublishRefusedError(
            f"{argv[0]} could not start ({type(exc).__name__})"
        ) from exc


@cache
def _report_policy() -> ModelDelegationReportPolicy:
    contract = Path(__file__).resolve().parents[1] / "contract.yaml"
    return ModelDelegationReportPolicy.model_validate(
        yaml.safe_load(contract.read_text(encoding="utf-8"))["delegation_report"]
    )


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.utcoffset() is None:
        raise ValueError("projection timestamp has no UTC offset")
    return stamp.astimezone(UTC)


def _rung(
    backend: str, tier: str, model: str, policy: ModelDelegationReportPolicy
) -> str:
    for group in policy.groups:
        if (
            tier in group.tiers
            or backend.startswith(group.backend_prefixes)
            or any(part in backend for part in group.backend_contains)
            or model.casefold().startswith(group.model_prefixes)
        ):
            return group.name
    return policy.fallback_group


def _outcome(attempt: dict[str, Any]) -> str:
    decision = attempt.get("acceptance_decision")
    if decision == "accept" or (
        decision is None
        and attempt.get("tier") == "harness"
        and attempt.get("quality_gate_passed") is True
    ):
        return "accepted"
    if decision in {"reject", "refuse", "refused"}:
        return "refused"
    if attempt.get("failure_class"):
        return "failed"
    return "unknown"


def _terminal_outcome(row: dict[str, Any]) -> str:
    if row.get("quality_gate_passed") is True and row.get("terminal_ok") is not False:
        return "accepted"
    if row.get("operational_outcome") in {
        "quality_rejected",
        "refused_before_dispatch",
    }:
        return "refused"
    if row.get("terminal_ok") is False:
        return "failed"
    return "unknown"


def _run_outcomes(outcomes: Mapping[str, set[str]]) -> dict[str, int]:
    counts = {
        f"{name}_runs": 0 for name in ("accepted", "refused", "failed", "unknown")
    }
    for values in outcomes.values():
        verdict = next(
            name
            for name in ("accepted", "refused", "failed", "unknown")
            if name in values
        )
        counts[f"{verdict}_runs"] += 1
    return counts


def delegation_report(
    snapshot: dict[str, Any],
    endpoints: list[dict[str, Any]],
    *,
    policy: ModelDelegationReportPolicy | None = None,
) -> dict[str, Any]:
    """Fold a complete projection read; absent or malformed data never means zero."""
    policy = _report_policy() if policy is None else policy
    now = _time(snapshot["now"])
    if not isinstance(snapshot.get("total_rows"), int) or snapshot["total_rows"] <= 0:
        raise ValueError("projection has no positive control")
    if not isinstance(snapshot.get("rows"), list):
        raise ValueError("projection rows are unread")
    windows: dict[str, Any] = {}
    for hours in (2, 24):
        runs: set[str] = set()
        unattributed: set[str] = set()
        groups: dict[tuple[str, str, str], dict[str, Any]] = {}
        members: dict[tuple[str, str, str], set[str]] = {}
        outcomes: dict[tuple[str, str, str], dict[str, set[str]]] = {}
        for row in snapshot["rows"]:
            if (
                row.get("data_source") != "real"
                or not now - timedelta(hours=hours) < _time(row["timestamp"]) <= now
            ):
                continue
            ident = row["correlation_id"]
            if not ident or ident in runs:
                continue
            runs.add(ident)
            attempts = row.get("attempt_history")
            if not isinstance(attempts, list):
                raise ValueError("projection attempt history is unread")
            if not attempts:
                unattributed.add(ident)
                # Historical terminals still identify an answering backend. They
                # count as runs, without inventing an attempt or its outcome.
                attempts = [
                    {
                        "backend_id": row.get("backend_id"),
                        "model_id": row.get("model_name"),
                        "host": row.get("host"),
                        "tier": row.get("cost_tier_name"),
                        "terminal_only": True,
                    }
                ]
            for attempt in attempts:
                backend = str(attempt.get("backend_id") or "")
                model = str(attempt.get("model_id") or "not recorded")
                host = str(
                    attempt.get("host")
                    or urlsplit(attempt.get("endpoint") or "").hostname
                    or "not recorded"
                )
                rung = _rung(backend, str(attempt.get("tier") or ""), model, policy)
                key = (rung, model, host)
                group = groups.setdefault(
                    key,
                    {
                        "rung": rung,
                        "model": model,
                        "host": host,
                        "runs": 0,
                        "attempts": 0,
                        "accepted": 0,
                        "refused": 0,
                        "failed": 0,
                        "unknown": 0,
                    },
                )
                members.setdefault(key, set()).add(ident)
                outcome = (
                    _terminal_outcome(row)
                    if attempt.get("terminal_only")
                    else _outcome(attempt)
                )
                outcomes.setdefault(key, {}).setdefault(ident, set()).add(outcome)
                if not attempt.get("terminal_only"):
                    group["attempts"] += 1
                    group[_outcome(attempt)] += 1
        for key, group in groups.items():
            group["runs"] = len(members[key])
            group.update(_run_outcomes(outcomes[key]))
        by_rung: dict[str, Any] = {}
        for rung in (*[group.name for group in policy.groups], policy.fallback_group):
            rung_outcomes: dict[str, set[str]] = {}
            totals = {
                "runs": 0,
                "attempts": 0,
                "accepted": 0,
                "refused": 0,
                "failed": 0,
                "unknown": 0,
            }
            for key, group in groups.items():
                if key[0] != rung:
                    continue
                for ident, values in outcomes[key].items():
                    rung_outcomes.setdefault(ident, set()).update(values)
                for name in ("attempts", "accepted", "refused", "failed", "unknown"):
                    totals[name] += group[name]
            totals["runs"] = len(rung_outcomes)
            totals.update(_run_outcomes(rung_outcomes))
            by_rung[rung] = totals
        windows[f"{hours}h"] = {
            "start": (now - timedelta(hours=hours)).isoformat(),
            "end": now.isoformat(),
            "total_runs": len(runs),
            "local_runs": by_rung[policy.local_group]["runs"],
            "unattributed_runs": len(unattributed),
            "by_rung": by_rung,
            "rungs": [groups[k] for k in sorted(groups)],
        }
    configured = [e for e in endpoints if not e.get("diagnostic_only")]
    unbound = sum(not e.get("endpoint_url") for e in configured)
    cause = (
        f"{unbound}/{len(configured)} configured local chat backends have unbound endpoints; the chain cannot call those backends."
        if unbound
        else "Compare projection attempts and endpoint inventory before assigning a cause."
    )
    if windows["24h"]["local_runs"] == 0:
        cause += " No real projection run tried a local rung in 24h."
    return {
        "schema": "delegation-floor/1",
        "status": "read",
        "generated_at": now.isoformat(),
        "positive_control_rows": snapshot["total_rows"],
        "latest_local": snapshot.get("latest_local"),
        "windows": windows,
        "endpoints": endpoints,
        "cause": cause,
        "outcomes": [],
        "why": cause,
    }


def tick_line(report: dict[str, Any], *, lanes_running: int) -> str:
    if report.get("status") != "read":
        return f"UNREAD delegation-local 2h reason={report.get('reason', 'projection unread')} FIX: read the lab delegation projection and its positive control"
    window = report["windows"]["2h"]
    local, total = window["local_runs"], window["total_runs"]
    status = "MISSING" if (lanes_running > 0 or total > 0) and local == 0 else "OK"
    line = f"{status} delegation-local 2h local={local}/{total} delegated lanes={lanes_running} newest_local={report.get('latest_local') or 'never recorded'}"
    return f"{line} FIX: {FIX}" if status == "MISSING" else line


class HandlerDelegationReport:
    """The existing publisher's read seam, also callable by the session tick."""

    def __init__(self, runner: Runner | None = None) -> None:
        self._run = run_process if runner is None else runner

    def handle(
        self,
        request: ModelDelegationReportRequest,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ModelDelegationReportResult:
        env = os.environ if environ is None else environ
        try:
            overlay = request.overlay or load_structured_overlay(
                "delegation_health",
                ModelDelegationReadOverlay,
                env,
                file_env="DELEGATION_HEALTH_OVERLAY_PATH",
            )
            host = env.get(overlay.ssh_host_env, "").strip()
            if not host:
                raise ValueError(f"{overlay.ssh_host_env} is unset")
            unit = 'unit() { docker ps --filter "label=com.docker.compose.project=$1" --filter "label=com.docker.compose.service=$2" --filter status=running --format \'{{.Names}}\' | head -1; }'
            script = "\n".join(
                [
                    "set -eu",
                    unit,
                    f"DB=$(unit {overlay.compose_project} {overlay.postgres_service})",
                    f"RT=$(unit {overlay.compose_project} {overlay.runtime_service})",
                    '[ -n "$DB" ] && [ -n "$RT" ]',
                    f"docker exec -i \"$DB\" psql -X -q -v ON_ERROR_STOP=1 -U {overlay.db_user} -d {overlay.database} -tA <<'SQL'",
                    READ_SQL,
                    "SQL",
                    "echo @@CONFIG",
                    f'docker exec -i "$RT" python3 - {"probe" if request.probe else "read"} '
                    + shlex.quote(
                        json.dumps([e.model_dump() for e in overlay.probe_endpoints])
                    )
                    + " <<'PY'",
                    CONFIG_PY,
                    "PY",
                ]
            )
            result = self._run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    "--",
                    host,
                    "bash -s",
                ],
                script.encode(),
                120,
            )
            if result.returncode:
                raise ValueError(f"lab projection read exited {result.returncode}")
            data, config = result.stdout.decode().split("@@CONFIG", 1)
            inventory = json.loads(config)
            report = delegation_report(json.loads(data), inventory["endpoints"])
            report["chains"] = inventory["chains"]
        except (
            OSError,
            PublishRefusedError,
            ValueError,
            KeyError,
            TypeError,
            NodeOverlayError,
            subprocess.TimeoutExpired,
        ) as exc:
            report = {
                "status": "unread",
                "generated_at": datetime.now(UTC).isoformat(),
                "reason": str(exc),
                "outcomes": [],
            }
        return ModelDelegationReportResult(
            report=report, tick=tick_line(report, lanes_running=request.lanes_running)
        )
