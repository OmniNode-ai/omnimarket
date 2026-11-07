# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 16's reading: planted-fact recall after a forced compaction, and eval runs that end in a
result (OMN-22980).

``handle(ModelContextRecallReadRequest) -> ModelContextRecallReading``. It reads no clock, file,
network or environment: the caller supplies the read time and the overlay. The rules:

* A probe or run counts when its time lies in ``[now - max_age, now]``. A later time is
  future-dated and fails the reading. Evidence that exists but is all older than the window is
  stale: NOT WORKING. No evidence supplied at all is UNKNOWN.
* A planted fact passes only when a compaction was forced and finished, the order is plant,
  compaction done, recall read, and the answer contains the planted text. The match is exact
  after collapsing whitespace and case, never a partial or fuzzy one.
* An eval run passes when it ended in a result. One that ended with no result fails; one that is
  open past the overlay's deadline fails; one open inside the deadline is in progress and counts
  as neither.
* WORKING needs every in-window probe and run to pass and the overlay's minimums to be met.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml
from pydantic import ValidationError

from omnimarket.models.model_context_recall_reading import (
    ModelContextEvalRunRead,
    ModelContextRecallProbeRead,
    ModelContextRecallReading,
    ModelContextRecallReadRequest,
    ModelEvalRun,
    ModelPlantedFactProbe,
)
from omnimarket.models.model_reading import EnumReadingVerdict

_NODE = "omnimarket.nodes.node_context_recall_reading_compute"


def load_topics() -> dict[str, str]:
    """The topics by role, from the node's contract."""
    contract = yaml.safe_load(files(_NODE).joinpath("contract.yaml").read_text())
    return dict(contract["topics"])


def _normal(text: str) -> str:
    return " ".join(text.lower().split())


def fact_recalled(expected: str, answer: str | None) -> bool:
    """The answer carries the planted fact, whitespace and case aside."""
    return answer is not None and _normal(expected) in _normal(answer)


def probe_reason(probe: ModelPlantedFactProbe) -> str | None:
    """None when the probe passes, else the reason code."""
    if not probe.compaction_forced or probe.compaction_completed_at is None:
        return f"compaction-not-completed:{probe.probe_id}"
    if probe.recall_read_at is not None and not (
        probe.planted_at <= probe.compaction_completed_at <= probe.recall_read_at
    ):
        return f"probe-out-of-order:{probe.probe_id}"
    if not fact_recalled(probe.expected_fact, probe.recalled_answer):
        return f"planted-fact-not-recalled:{probe.probe_id}"
    return None


def eval_run_reason(
    run: ModelEvalRun, now: datetime, deadline_seconds: int
) -> str | None:
    """None when the run ended in a result or is still inside its deadline, else the reason."""
    if run.ended_at is not None:
        return None if run.result_id else f"eval-ended-without-result:{run.run_id}"
    if now - run.started_at > timedelta(seconds=deadline_seconds):
        return f"eval-open-past-deadline:{run.run_id}"
    return None


def _in_window(moment: datetime, now: datetime, max_age_seconds: int) -> bool:
    return now - timedelta(seconds=max_age_seconds) <= moment <= now


class HandlerContextRecallReading:
    """Compute piece 16's reading at the read time."""

    def handle(
        self, request: ModelContextRecallReadRequest
    ) -> ModelContextRecallReading:
        now = request.now
        overlay = request.overlay
        reasons: list[str] = []
        probe_reads: list[ModelContextRecallProbeRead] = []
        run_reads: list[ModelContextEvalRunRead] = []
        passing_probes = 0
        finished_runs = 0
        probes_in_window = 0
        runs_in_window = 0

        reason: str | None
        for probe in request.probes:
            if probe.read_at > now:
                reason = f"future-dated:{probe.probe_id}"
                reasons.append(reason)
                probe_reads.append(
                    ModelContextRecallProbeRead(
                        probe_id=probe.probe_id,
                        in_window=False,
                        recalled=False,
                        reason=reason,
                    )
                )
                continue
            inside = _in_window(probe.read_at, now, overlay.max_age_seconds)
            reason = probe_reason(probe) if inside else None
            probes_in_window += inside
            recalled = inside and reason is None
            if reason is not None:
                reasons.append(reason)
            passing_probes += recalled
            probe_reads.append(
                ModelContextRecallProbeRead(
                    probe_id=probe.probe_id,
                    in_window=inside,
                    recalled=recalled,
                    reason=reason,
                )
            )

        for run in request.eval_runs:
            if run.started_at > now:
                reason = f"future-dated:{run.run_id}"
                reasons.append(reason)
                run_reads.append(
                    ModelContextEvalRunRead(
                        run_id=run.run_id,
                        in_window=False,
                        ended_in_result=False,
                        in_progress=False,
                        reason=reason,
                    )
                )
                continue
            inside = _in_window(run.started_at, now, overlay.max_age_seconds)
            reason = (
                eval_run_reason(run, now, overlay.eval_run_deadline_seconds)
                if inside
                else None
            )
            ended = inside and run.ended_at is not None and bool(run.result_id)
            in_progress = inside and run.ended_at is None and reason is None
            runs_in_window += inside
            if reason is not None:
                reasons.append(reason)
            finished_runs += ended
            run_reads.append(
                ModelContextEvalRunRead(
                    run_id=run.run_id,
                    in_window=inside,
                    ended_in_result=ended,
                    in_progress=in_progress,
                    reason=reason,
                )
            )

        unknown = False
        if not request.probes:
            reasons.append("no-probe-supplied")
            unknown = True
        elif probes_in_window == 0 and not any(
            r.startswith("future-dated") for r in reasons
        ):
            reasons.append("stale-probe:no-probe-in-window")
        if not request.eval_runs:
            reasons.append("no-eval-run-supplied")
            unknown = True
        elif runs_in_window == 0 and not any(
            r.startswith("future-dated") for r in reasons
        ):
            reasons.append("stale-eval-run:no-eval-run-in-window")

        if not reasons:
            if passing_probes < overlay.min_probes:
                reasons.append(
                    f"below-minimum:probes:{passing_probes}-of-{overlay.min_probes}"
                )
            if finished_runs < overlay.min_eval_runs:
                reasons.append(
                    f"below-minimum:eval-runs:{finished_runs}-of-{overlay.min_eval_runs}"
                )

        # A fault found beats a missing input: UNKNOWN only when nothing else is wrong.
        only_missing = unknown and all(r.startswith("no-") for r in reasons)
        if not reasons:
            verdict = EnumReadingVerdict.WORKING
        elif only_missing:
            verdict = EnumReadingVerdict.UNKNOWN
        else:
            verdict = EnumReadingVerdict.NOT_WORKING

        return ModelContextRecallReading(
            correlation_id=request.correlation_id,
            verdict=verdict,
            reasons=reasons,
            probed_at=now,
            max_age_seconds=overlay.max_age_seconds,
            overlay_hash=overlay.content_hash(),
            probe_reads=probe_reads,
            eval_run_reads=run_reads,
            probes_recalled=passing_probes,
            eval_runs_with_result=finished_runs,
        )


def events_of(
    reading: ModelContextRecallReading, topics: dict[str, str]
) -> list[tuple[str, dict[str, Any]]]:
    """The events one reading produces, in order: each probe, each run, then the reading."""
    cid = reading.correlation_id
    events: list[tuple[str, dict[str, Any]]] = [
        (topics["probe_read"], {"correlation_id": cid, **p.model_dump(mode="json")})
        for p in reading.probe_reads
    ]
    events += [
        (topics["eval_run_read"], {"correlation_id": cid, **r.model_dump(mode="json")})
        for r in reading.eval_run_reads
    ]
    events.append((topics["reading_completed"], reading.model_dump(mode="json")))
    return events


def answer_command(
    payload: bytes, handler: HandlerContextRecallReading, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a request, and a handler that raises, are each answered with a typed
    failure event, never dropped.
    """
    try:
        request = ModelContextRecallReadRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {
            "correlation_id": _correlation_of(payload),
            "failure": f"bad-payload:{fields}",
        }
        return [(topics["reading_failed"], json.dumps(failure).encode())]
    try:
        reading = handler.handle(request)
    except Exception as exc:  # the chain answers a fault with a typed event
        failure = {
            "correlation_id": request.correlation_id,
            "failure": f"handler-error:{type(exc).__name__}",
        }
        return [(topics["reading_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(reading, topics)
    ]


def _correlation_of(payload: bytes) -> str:
    """The correlation id of a payload that failed validation, or ``unknown``."""
    try:
        loaded = json.loads(payload)
    except ValueError:
        return "unknown"
    cid = loaded.get("correlation_id") if isinstance(loaded, dict) else None
    return cid if isinstance(cid, str) and cid else "unknown"


__all__ = [
    "HandlerContextRecallReading",
    "answer_command",
    "eval_run_reason",
    "events_of",
    "fact_recalled",
    "load_topics",
    "probe_reason",
]
