# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures and an in-memory store for the eval-results projection tests (OMN-24171).

``MemoryStore`` follows the SQL of ``EvalResultsStore`` statement for statement: an item is replaced
only when it supersedes the standing row, a trace part is inserted and never replaced, a run's
rates are deleted for the scope and written again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from omnimarket.events.context_roi import (
    ModelAttemptReductionRow,
    ModelContextRoiRunResult,
)
from omnimarket.models.eval_item.model_eval_item_scored import (
    event_id_of,
    scoring_id_of,
)
from omnimarket.models.eval_item.model_eval_item_traced import (
    DEFAULT_TRACE_BOUND,
    ModelEvalItemTrace,
    ModelEvalTraceBound,
    split_trace,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    ModelEvalProjectionResult,
    ModelEvalReadPlan,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    ModelEvalItemRow,
    ModelEvalRateRow,
    ModelEvalRunRecord,
    ModelEvalTraceRow,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalResultsOverlay,
)

TENANT = "11111111-1111-1111-1111-111111111111"
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
MODEL = "local-coder"


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


def overlay(**updates: Any) -> ModelEvalResultsOverlay:
    values: dict[str, Any] = {
        "display_names": {MODEL: "Local coding model"},
        "max_models": 8,
        "snapshot_runs": 26,
        "live_weeks": 26,
        "max_field_chars": 200,
        "snapshot_max_bytes": 900_000,
        "publish_interval_s": 600,
        "lane": "dev",
        "host": "h202",
        "install_revision": "rev-1",
    }
    return ModelEvalResultsOverlay(**(values | updates))


def try_row(
    run_id: str,
    task: str,
    *,
    model: str = MODEL,
    ordinal: int = 1,
    verdict: str = "pass",
    source: str | None = None,
    band: str = "S",
    revision: str = "sha256:rev-a",
    **extra: Any,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "run_id": run_id,
        "correlation_id": f"{run_id}|{task}|{model}|{ordinal}",
        "task_id": task,
        "model_id": model,
        "check_verdict": verdict,
        "unscorable_source": source,
        "size_band": band,
        "try_ordinal": ordinal,
        "suite_digest": "suite-digest",
        "model_revision": revision,
        "generation_settings_hash": "settings-1",
        "prompt_template_version": "prompt-v1",
        "scorer_version": "scorer-v1",
        "context_factor_subset": "off",
    }
    return ModelAttemptReductionRow(**(fields | extra)).model_dump(mode="json")


def code_event(run_id: str, *rows: dict[str, Any]) -> dict[str, Any]:
    return ModelContextRoiRunResult(
        run_id=run_id,
        rows=tuple(ModelAttemptReductionRow.model_validate(r) for r in rows),
        total_trials=len(rows),
    ).model_dump(mode="json")


def close_event(
    run_id: str,
    sequences: tuple[tuple[str, str, str], ...],
    *,
    void_reason: str | None = None,
    series: str = "tracking",
    closed_at: datetime | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A close as the projection reads it: the producer's fields beside the ones a later producer adds."""
    return {
        "correlation_id": run_id,
        "tenant_id": "example-tenant",
        "closed_at": (closed_at or at(30)).isoformat(),
        "abandoned": False,
        "void_reason": void_reason,
        "sequences": [
            {
                "task_key": t,
                "model": m,
                "band": b,
                "backend_id": "backend",
                "verdicts": ["pass"],
                "duration_s": 1.0,
            }
            for t, m, b in sequences
        ],
        "series": series,
        "task_sample_digest": "sample-digest",
        **extra,
    }


def judged_event(
    call: str,
    *,
    model: str = MODEL,
    accept: bool = True,
    call_time: datetime | None = None,
    judged_at: datetime | None = None,
    rubric: str = "v1",
    judge_version: str = "judge-1",
    origin: str = "live",
    eval_run_id: str | None = None,
    tier: str = "local",
) -> dict[str, Any]:
    from omnimarket.models.delegation_acceptance_judge.model_delegation_acceptance_judged_event import (
        ModelDelegationAcceptanceJudgedEvent,
    )

    return json.loads(
        ModelDelegationAcceptanceJudgedEvent(
            correlation_id=UUID(call),
            tenant_id=UUID(TENANT),
            delegated_model_key=model,
            delegated_tier=tier,
            task_type="code",
            kind="task",
            call_time=call_time or at(1),
            judge_run_id=f"judge-{rubric}-{judge_version}",
            judge_model="judge",
            judge_model_version=judge_version,
            rubric_id="rubric",
            rubric_version=rubric,
            rubric_hash="hash",
            calibration_run_id="cal",
            calibration_n=20,
            calibration_agreement=0.9,
            calibration_kappa=0.8,
            accept=accept,
            quality=2,
            failure_class="none",
            judged_at=judged_at or at(2),
            origin=origin,
            eval_run_id=eval_run_id,
        ).model_dump_json()
    )


def trace_parts(
    run_id: str,
    task: str,
    *,
    ordinal: int = 1,
    model: str = MODEL,
    scored_at: datetime | None = None,
    text: str = "the task as sent",
    bound: ModelEvalTraceBound = DEFAULT_TRACE_BOUND,
) -> tuple[dict[str, Any], ...]:
    event_id = event_id_of(
        scorer="contract_checks",
        run_id=run_id,
        task_id=task,
        engine="local",
        model_id=model,
        context_arm="off",
        replicate=0,
        try_ordinal=ordinal,
    )
    trace = ModelEvalItemTrace(input_text=text, response_text=f"answer to {task}")
    scoring = scoring_id_of(
        event_id=event_id, scored_at=scored_at or at(3), trace_sha256=trace.sha256()
    )
    return tuple(
        p.model_dump(mode="json")
        for p in split_trace(trace, event_id=event_id, scoring_id=scoring, bound=bound)
    )


@dataclass(frozen=True)
class StoredTables:
    """Every stored row of the four tables as canonical JSON, sorted."""

    items: tuple[str, ...]
    parts: tuple[str, ...]
    runs: tuple[str, ...]
    rates: tuple[str, ...]


class MemoryStore:
    """The four stores in memory, with the semantics of the SQL in ``EvalResultsStore``."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], ModelEvalItemRow] = {}
        self.parts: dict[tuple[str, str, str, int], ModelEvalTraceRow] = {}
        self.runs: dict[tuple[str, str], ModelEvalRunRecord] = {}
        self.rates: list[ModelEvalRateRow] = []

    async def load(self, plan: ModelEvalReadPlan) -> Any:
        items = tuple(
            i
            for (tenant, _), i in self.items.items()
            if tenant == plan.tenant_id
            and plan.run_id
            and i.run_id == plan.run_id
            and (not plan.model_id or i.model_id == plan.model_id)
        )
        record = self.runs.get((plan.tenant_id, plan.run_id)) if plan.run_id else None
        parts = tuple(
            p
            for p in self.parts.values()
            if plan.scoring_id
            and p.tenant_id == plan.tenant_id
            and str(p.scoring_id) == plan.scoring_id
        )
        return items, record, parts

    async def apply(
        self, plan: ModelEvalReadPlan, result: ModelEvalProjectionResult
    ) -> dict[str, int]:
        counts = {"items": 0, "traces": 0, "run": 0, "rates": 0}
        for scoring in result.drop_scoring_ids:
            for key in [
                k for k in self.parts if k[0] == plan.tenant_id and k[2] == scoring
            ]:
                del self.parts[key]
        for item in result.items:
            key = (item.tenant_id, item.item_key)
            held = self.items.get(key)
            if held is None or item.supersedes(held):
                self.items[key] = item
                counts["items"] += 1
        for part in result.traces:
            key = (part.tenant_id, part.item_key, str(part.scoring_id), part.part_index)
            if key not in self.parts:
                self.parts[key] = part
                counts["traces"] += 1
        if result.run_record is not None:
            key = (result.run_record.tenant_id, result.run_record.run_id)
            held_run = self.runs.get(key)
            if held_run is None or held_run.closed_at < result.run_record.closed_at:
                self.runs[key] = result.run_record
                counts["run"] += 1
        if plan.run_id and (result.rates or result.items or result.run_record):
            self.rates = [
                r
                for r in self.rates
                if not (
                    r.run_id == plan.run_id
                    and (not plan.model_id or r.model_id == plan.model_id)
                )
            ] + list(result.rates)
            counts["rates"] += len(result.rates)
        return counts

    async def snapshot_inputs(self, tenant_id: str) -> Any:
        rates = tuple(
            r
            for r in self.rates
            if r.tenant_id == tenant_id
            and (r.series == "tracking" or r.run_state.value == "live")
        )
        records = tuple(
            r
            for r in self.runs.values()
            if r.tenant_id == tenant_id and r.series == "tracking" and r.void_reason
        )
        return rates, records

    def tables(self) -> StoredTables:
        """A canonical, order-free picture of the tables, to compare across replays."""
        return StoredTables(
            items=tuple(sorted(i.model_dump_json() for i in self.items.values())),
            parts=tuple(sorted(p.model_dump_json() for p in self.parts.values())),
            runs=tuple(sorted(r.model_dump_json() for r in self.runs.values())),
            rates=tuple(sorted(r.model_dump_json() for r in self.rates)),
        )


class FakeDb:
    """The adapter surface the writer touches around the store."""

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None


def make_writer(
    store: MemoryStore,
    *,
    overlay_: ModelEvalResultsOverlay | None = None,
    clock: Any = None,
) -> tuple[Any, list[tuple[str, dict[str, Any]]]]:
    """A writer over ``store`` with its publish transport captured in the returned list."""
    from omnimarket.nodes.node_projection_eval_results.handlers.handler_eval_results_writer import (
        EvalResultsProjectionWriter,
    )

    writer = EvalResultsProjectionWriter(
        overlay=overlay_ or overlay(),
        clock=clock or (lambda: at(100)),
        store_factory=lambda _db: store,
    )
    writer._db = FakeDb()  # type: ignore[assignment]
    published: list[tuple[str, dict[str, Any]]] = []

    async def publish(topic: str, value: bytes) -> None:
        published.append((topic, json.loads(value)))

    writer._publish_fn = publish
    return writer, published


async def deliver(
    writer: Any,
    topic: str,
    payload: dict[str, Any],
    when: datetime,
    tenant: str = TENANT,
) -> dict[str, Any]:
    from omnimarket.projection.runner import MessageMeta

    return await writer._project(
        topic,
        {**payload, "_envelope_timestamp": when.isoformat(), "_tenant_id": tenant},
        MessageMeta(partition=0, offset=0, fallback_id="", topic=topic),
    )
