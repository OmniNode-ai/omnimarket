# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerFloorDelegationHistory: the delegation history document as a pure fold.

Definition-B compute: ``handle(request: ModelHistoryRequest) -> ModelDelegationHistory``. No file, no clock, no network, no
model. The same request in any row order gives byte-identical output.

The readings come from the effect node: attempts and latencies per hour from the lab projection, first and last seen per model,
the routing overlay's backend settings and the experiment reports' result tables. The overlay's policy says which backend is
which engine, which runs never reached a model, the serving settings no routing entry carries as data, and which backend an
experiment arm's label means. Nothing here names a model, a host or a prefix.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from omnimarket.events.floor_delegation_history import (
    SCHEMA_ID,
    EngineKind,
    ModelArm,
    ModelBaseline,
    ModelDelegationHistory,
    ModelExperiment,
    ModelHistoryCounts,
    ModelHistoryModel,
    ModelHistorySource,
    ModelPairedTest,
    ModelSeries,
    ModelSeriesPoint,
    ModelSuccess,
    ModelTaskSet,
    Thinking,
)
from omnimarket.events.floor_history_request import (
    ModelAttemptCounts,
    ModelBackendSettings,
    ModelHistoryPolicy,
    ModelHistoryRequest,
    ModelImportedExperiment,
    ModelLatencyRead,
    ModelSeenRead,
)

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_WILSON_Z = 1.96
ALL_TASK_KINDS = "*"
UNNAMED_TASK_KIND = "unknown"
ModelOf = Callable[[str, str, str], str]


def wilson(correct: int, n: int) -> tuple[float | None, float | None]:
    """The Wilson score interval at 95 percent for ``correct`` of ``n``; (None, None) when ``n`` is zero."""
    if n <= 0:
        return None, None
    p = correct / n
    z2 = _WILSON_Z * _WILSON_Z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = (_WILSON_Z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / (1 + z2 / n)
    return round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)


def _success(accepted: int, attempts: int) -> ModelSuccess:
    low, high = wilson(accepted, attempts)
    rate = round(accepted / attempts, 4) if attempts else None
    return ModelSuccess(rate=rate, wilson_low=low, wilson_high=high)


def _nearest_rank(sorted_values: list[int], fraction: float) -> float:
    return float(sorted_values[math.ceil(fraction * len(sorted_values)) - 1])


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(UTC)


def _engine_kind(
    backend_id: str, model_name: str, policy: ModelHistoryPolicy
) -> EngineKind:
    folded = model_name.casefold()
    for rule in policy.engine_rules:
        if rule.backend_prefixes and backend_id.startswith(rule.backend_prefixes):
            return rule.kind
        if rule.model_prefixes and folded.startswith(
            tuple(p.casefold() for p in rule.model_prefixes)
        ):
            return rule.kind
    return policy.fallback_engine


def _model_id(kind: EngineKind, model_name: str, host: str) -> str:
    return f"{model_name}@{host}" if kind == "local" else model_name


class _Settings:
    """The serving settings of one model, gathered from the routing entries and the overlay's declarations."""

    def __init__(self) -> None:
        self.context_tokens: int | None = None
        self.price_in: float | None = None
        self.price_out: float | None = None
        self.quantization: str | None = None
        self.thinking: Thinking = "unrecorded"
        self.sources: set[str] = set()


def _settings_for(
    backend_ids: list[str],
    backends: dict[str, ModelBackendSettings],
    serving: dict[str, tuple[str | None, Thinking, str]],
) -> _Settings:
    out = _Settings()
    for backend_id in sorted(backend_ids):
        routed = backends.get(backend_id)
        if routed is not None:
            used = False
            if out.context_tokens is None and routed.context_tokens is not None:
                out.context_tokens, used = routed.context_tokens, True
            if out.price_in is None and routed.price_in_usd_per_mtok is not None:
                out.price_in, used = routed.price_in_usd_per_mtok, True
            if out.price_out is None and routed.price_out_usd_per_mtok is not None:
                out.price_out, used = routed.price_out_usd_per_mtok, True
            if used:
                out.sources.add(f"routing:{backend_id}")
        declared = serving.get(backend_id)
        if declared is not None:
            quantization, thinking, recorded_in = declared
            used = False
            if out.quantization is None and quantization is not None:
                out.quantization, used = quantization, True
            if out.thinking == "unrecorded" and thinking != "unrecorded":
                out.thinking, used = thinking, True
            if used:
                out.sources.add(f"serving:{recorded_in}")
    return out


def _settings_line(model: ModelHistoryModel) -> str:
    parts: list[str] = []
    if model.context_tokens is not None:
        parts.append(f"context {model.context_tokens} tokens")
    parts.append(f"quantization {model.quantization or 'unrecorded'}")
    parts.append(f"thinking {model.thinking}")
    if (
        model.price_in_usd_per_mtok is not None
        and model.price_out_usd_per_mtok is not None
    ):
        parts.append(
            f"price {model.price_in_usd_per_mtok:g}/{model.price_out_usd_per_mtok:g} USD per Mtok"
        )
    return "; ".join(parts)


@dataclass
class _Group:
    """The readings of one model id: where it was seen and through which backends."""

    model_name: str
    kind: EngineKind
    host: str | None
    backends: set[str] = field(default_factory=set)
    first: list[str] = field(default_factory=list)
    last: list[str] = field(default_factory=list)


class _Cell:
    """Counts and latencies of one model, task kind and bucket."""

    def __init__(self) -> None:
        self.attempts = 0
        self.accepted = 0
        self.timeouts = 0
        self.cost = 0.0
        self.latencies: list[int] = []

    def point(self, bucket: str) -> ModelSeriesPoint:
        ordered = sorted(self.latencies)
        return ModelSeriesPoint(
            bucket=bucket,
            attempts=self.attempts,
            accepted=self.accepted,
            rejected=self.attempts - self.accepted - self.timeouts,
            timeouts=self.timeouts,
            latency_runs=len(ordered),
            p50_latency_ms=_nearest_rank(ordered, 0.5) if ordered else None,
            p95_latency_ms=_nearest_rank(ordered, 0.95) if ordered else None,
            cost_usd=round(self.cost, 6),
            success=_success(self.accepted, self.attempts),
        )

    def add(self, other: _Cell) -> None:
        self.attempts += other.attempts
        self.accepted += other.accepted
        self.timeouts += other.timeouts
        self.cost += other.cost
        self.latencies.extend(other.latencies)


def _day(bucket: str) -> str:
    return f"{bucket[:10]}T00:00:00Z"


class HandlerFloorDelegationHistory:
    """Folds the readings of one run into the delegation history document."""

    def handle(self, request: ModelHistoryRequest) -> ModelDelegationHistory:
        policy = request.policy
        skip = set(policy.not_a_model_backend_ids)
        no_model = tuple(policy.not_a_model_name_prefixes)

        def is_model(backend_id: str, model_name: str) -> bool:
            return backend_id not in skip and not (
                no_model and model_name.startswith(no_model)
            )

        attempts = [a for a in request.attempts if is_model(a.backend_id, a.model_name)]
        latencies = [
            x for x in request.latencies if is_model(x.backend_id, x.model_name)
        ]
        seen = [s for s in request.seen if is_model(s.backend_id, s.model_name)]

        backends = {b.backend_id: b for b in request.backends}
        serving = {
            d.backend_id: (d.quantization, d.thinking, d.recorded_in)
            for d in policy.serving
        }

        def model_of(backend_id: str, model_name: str, host: str) -> str:
            return _model_id(
                _engine_kind(backend_id, model_name, policy), model_name, host
            )

        models = self._models(request, attempts, latencies, seen, backends, serving)
        series = self._series(attempts, latencies, model_of, request.now, policy)
        experiments, extra_sources, arm_models = self._experiments(
            request, backends, serving, model_of
        )
        known = {m.id for m in models}
        merged = list(models)
        for arm_model in arm_models:
            if arm_model.id not in known:
                known.add(arm_model.id)
                merged.append(arm_model)
        merged.sort(key=lambda m: m.id)
        return ModelDelegationHistory(
            schema_id=SCHEMA_ID,
            generated_at=request.now,
            window_days=policy.window_days,
            models=tuple(merged),
            series=series,
            experiments=experiments,
            sources=(*request.sources, *extra_sources),
            counts=ModelHistoryCounts(
                models=len(merged),
                series=len(series),
                experiments=len(experiments),
                attempts_in_window=sum(a.attempts for a in attempts),
            ),
        )

    # ---- models ---------------------------------------------------------------------------------------------------------------

    def _models(
        self,
        request: ModelHistoryRequest,
        attempts: list[ModelAttemptCounts],
        latencies: list[ModelLatencyRead],
        seen: list[ModelSeenRead],
        backends: dict[str, ModelBackendSettings],
        serving: dict[str, tuple[str | None, Thinking, str]],
    ) -> list[ModelHistoryModel]:
        policy = request.policy
        groups: dict[str, _Group] = {}

        def group(backend_id: str, model_name: str, host: str) -> _Group:
            kind = _engine_kind(backend_id, model_name, policy)
            key = _model_id(kind, model_name, host)
            entry = groups.get(key)
            if entry is None:
                entry = _Group(
                    model_name=model_name,
                    kind=kind,
                    host=host if kind == "local" else None,
                )
                groups[key] = entry
            entry.backends.add(backend_id)
            return entry

        for a in attempts:
            entry = group(a.backend_id, a.model_name, a.host)
            entry.first.append(a.bucket)
            entry.last.append(a.bucket)
        for x in latencies:
            entry = group(x.backend_id, x.model_name, x.host)
            entry.first.append(x.bucket)
            entry.last.append(x.bucket)
        # first and last seen are the whole projection's; only a model with activity in the window is listed
        active = set(groups)
        for s in seen:
            kind = _engine_kind(s.backend_id, s.model_name, policy)
            key = _model_id(kind, s.model_name, s.host)
            if key in active:
                entry = group(s.backend_id, s.model_name, s.host)
                entry.first.append(s.first_seen)
                entry.last.append(s.last_seen)
        out: list[ModelHistoryModel] = []
        for key, entry in groups.items():
            ids = sorted(entry.backends)
            settings = _settings_for(ids, backends, serving)
            out.append(
                ModelHistoryModel(
                    id=key,
                    model_name=entry.model_name,
                    engine_kind=entry.kind,
                    host=entry.host,
                    backend_ids=tuple(ids),
                    context_tokens=settings.context_tokens,
                    quantization=settings.quantization,
                    thinking=settings.thinking,
                    price_in_usd_per_mtok=settings.price_in,
                    price_out_usd_per_mtok=settings.price_out,
                    settings_source=", ".join(sorted(settings.sources)) or "unrecorded",
                    first_seen=min(entry.first),
                    last_seen=max(entry.last),
                )
            )
        out.sort(key=lambda m: m.id)
        return out

    # ---- series ---------------------------------------------------------------------------------------------------------------

    def _series(
        self,
        attempts: list[ModelAttemptCounts],
        latencies: list[ModelLatencyRead],
        model_of: ModelOf,
        now: str,
        policy: ModelHistoryPolicy,
    ) -> tuple[ModelSeries, ...]:
        hours: dict[tuple[str, str, str], _Cell] = defaultdict(_Cell)
        for a in attempts:
            cell = hours[
                (
                    model_of(a.backend_id, a.model_name, a.host),
                    a.task_type or UNNAMED_TASK_KIND,
                    a.bucket,
                )
            ]
            cell.attempts += a.attempts
            cell.accepted += a.accepted
            cell.timeouts += a.timeouts
            cell.cost += a.cost_usd
        for x in latencies:
            cell = hours[
                (
                    model_of(x.backend_id, x.model_name, x.host),
                    x.task_type or UNNAMED_TASK_KIND,
                    x.bucket,
                )
            ]
            cell.latencies.extend(x.latencies_ms)
        # every task kind of a model together
        for (model, _task, bucket), cell in list(hours.items()):
            hours[(model, ALL_TASK_KINDS, bucket)].add(cell)
        window_start = (_parse(now) - timedelta(days=policy.window_days)).replace(
            minute=0, second=0, microsecond=0
        )
        by_series: dict[tuple[str, str], list[str]] = defaultdict(list)
        for model, task, bucket in hours:
            by_series[(model, task)].append(bucket)
        out: list[ModelSeries] = []
        for (model, task), buckets in sorted(by_series.items()):
            day_cells: dict[str, _Cell] = defaultdict(_Cell)
            total = _Cell()
            hour_points: list[ModelSeriesPoint] = []
            for bucket in sorted(buckets):
                cell = hours[(model, task, bucket)]
                hour_points.append(cell.point(bucket))
                day_cells[_day(bucket)].add(cell)
                total.add(cell)
            out.append(
                ModelSeries(
                    model=model,
                    task_kind=task,
                    days=tuple(day_cells[d].point(d) for d in sorted(day_cells)),
                    hours=tuple(hour_points),
                    total=total.point(window_start.strftime(_STAMP)),
                )
            )
        return tuple(out)

    # ---- experiments ----------------------------------------------------------------------------------------------------------

    def _experiments(
        self,
        request: ModelHistoryRequest,
        backends: dict[str, ModelBackendSettings],
        serving: dict[str, tuple[str | None, Thinking, str]],
        model_of: ModelOf,
    ) -> tuple[
        tuple[ModelExperiment, ...], list[ModelHistorySource], list[ModelHistoryModel]
    ]:
        policy = request.policy
        extra: list[ModelHistorySource] = []
        arm_models: dict[str, ModelHistoryModel] = {}
        flagged: set[str] = set()
        out: list[ModelExperiment] = []
        for imported in request.experiments:
            arms: list[ModelArm] = []
            n = max(arm.n for arm in imported.arms)
            for arm in imported.arms:
                model_id, settings = self._arm_model(
                    arm.label,
                    imported,
                    backends,
                    serving,
                    model_of,
                    arm_models,
                    extra,
                    flagged,
                    policy,
                )
                low, high = wilson(arm.correct, arm.n)
                if low is None or high is None:
                    raise ValueError(f"arm {arm.label} has no attempts")
                arms.append(
                    ModelArm(
                        label=arm.label,
                        model=model_id,
                        settings=settings,
                        correct=arm.correct,
                        n=arm.n,
                        accuracy=round(arm.correct / arm.n, 4),
                        wilson_low=low,
                        wilson_high=high,
                        p50_latency_s=arm.p50_latency_s,
                        gpu_seconds=arm.gpu_seconds,
                    )
                )
            base = imported.baseline
            out.append(
                ModelExperiment(
                    id=imported.id,
                    question=imported.question,
                    source=imported.source,
                    task_set=ModelTaskSet(
                        description=imported.task_set_description,
                        hash=imported.task_set_hash,
                    ),
                    n=n,
                    majority_baseline=None
                    if base is None
                    else ModelBaseline(
                        label=base.label,
                        correct=base.correct,
                        n=base.n,
                        accuracy=round(base.correct / base.n, 4),
                    ),
                    arms=tuple(arms),
                    paired=tuple(
                        ModelPairedTest(
                            test=p.test,
                            arm_a=p.arm_a,
                            arm_b=p.arm_b,
                            only_a_right=p.only_a_right,
                            only_b_right=p.only_b_right,
                            p_value=p.p_value,
                        )
                        for p in imported.paired
                    ),
                    verdict=imported.verdict,
                    date=imported.date,
                    date_source=imported.date_source,
                )
            )
        out.sort(key=lambda e: e.id)
        out.sort(key=lambda e: e.date, reverse=True)
        return tuple(out), extra, list(arm_models.values())

    def _arm_model(
        self,
        label: str,
        imported: ModelImportedExperiment,
        backends: dict[str, ModelBackendSettings],
        serving: dict[str, tuple[str | None, Thinking, str]],
        model_of: ModelOf,
        arm_models: dict[str, ModelHistoryModel],
        extra: list[ModelHistorySource],
        flagged: set[str],
        policy: ModelHistoryPolicy,
    ) -> tuple[str | None, str | None]:
        rule = next(
            (r for r in policy.arm_rules if re.search(r.label_pattern, label)), None
        )
        if rule is None:
            return None, None
        routed = backends.get(rule.backend_id)
        if routed is None:
            if rule.label_pattern not in flagged:
                flagged.add(rule.label_pattern)
                extra.append(
                    ModelHistorySource(
                        id=f"arm_rule:{rule.label_pattern}",
                        ok=False,
                        detail=f"backend {rule.backend_id} is not in the routing overlay",
                    )
                )
            return None, None
        kind = _engine_kind(routed.backend_id, routed.model_name, policy)
        model_id = _model_id(kind, routed.model_name, routed.host or "unknown")
        settings = _settings_for([routed.backend_id], backends, serving)
        model = ModelHistoryModel(
            id=model_id,
            model_name=routed.model_name,
            engine_kind=kind,
            host=(routed.host or "unknown") if kind == "local" else None,
            backend_ids=(routed.backend_id,),
            context_tokens=settings.context_tokens,
            quantization=settings.quantization,
            thinking=settings.thinking,
            price_in_usd_per_mtok=settings.price_in,
            price_out_usd_per_mtok=settings.price_out,
            settings_source=", ".join(sorted(settings.sources)) or "unrecorded",
            first_seen=imported.date,
            last_seen=imported.date,
        )
        arm_models.setdefault(model_id, model)
        return model_id, _settings_line(model)
