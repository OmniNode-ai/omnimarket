# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cost-and-throughput effect: read the floor's recorded documents, fold them, write the panel.

A step of the factory floor publish, after the simple floor step. The fold is ``node_floor_cost_throughput_compute``; this node makes
the readings and writes ``cost-throughput.json``. A document that is absent or unparsable is not passed to the fold, so every figure
that depends on it reads UNREADABLE. The integration record's cumulative counts are kept as dated readings in a file of this node's
own (one per new ``as_of``), because a per-hour rate needs two readings and the floor keeps only the latest.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.floor_cost_throughput import (
    ModelCostThroughputRequest,
    ModelDayOverDayReport,
    ModelSeenRunningPoint,
    ModelSeenRunningSeries,
    ModelSourceDocument,
)
from omnimarket.nodes.node_floor_cost_throughput_compute.handlers.handler_floor_cost_throughput import (
    HandlerFloorCostThroughput as PanelFold,
)
from omnimarket.nodes.node_floor_cost_throughput_effect.models.model_floor_cost_throughput_effect import (
    ModelCostThroughputRuntime,
    ModelFloorCostThroughputRequest,
    ModelFloorCostThroughputResult,
)

OWN_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
FLOOR_SIMPLE_NODE = "node_floor_simple_effect"
DELEGATION_NODE = "node_floor_delegation_history_compute"
SEEN_READ = "floor-simple.json integration.counts.seen_running at integration.as_of (kept readings)"


def load_runtime(contract_path: Path = OWN_CONTRACT) -> ModelCostThroughputRuntime:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelCostThroughputRuntime.model_validate(raw["runtime"])


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _load(path: Path) -> tuple[dict[str, Any], str] | None:
    """The parsed JSON object and its modified time as a UTC stamp, or None when the file is absent or not a JSON object."""
    try:
        data = json.loads(path.read_bytes())
        stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime(_STAMP)
    except (OSError, ValueError):
        return None
    return (data, stamp) if isinstance(data, dict) else None


class HandlerFloorCostThroughputEffect:
    """Runs the cost-and-throughput step."""

    def __init__(
        self,
        runtime: ModelCostThroughputRuntime | None = None,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._rt = runtime or load_runtime()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._environ = os.environ if environ is None else environ

    def handle(
        self, request: ModelFloorCostThroughputRequest
    ) -> ModelFloorCostThroughputResult:
        rt = self._rt
        home_text = request.home or self._environ.get(rt.home_env, "")
        if not home_text:
            return ModelFloorCostThroughputResult(
                ok=False, detail=f"no registry home: pass --home or set {rt.home_env}"
            )
        data_dir = Path(home_text) / rt.data_dir_relpath
        now = self._clock().astimezone(UTC)

        simple = _load(data_dir / rt.floor_simple_name)
        history = _load(data_dir / rt.delegation_history_name)
        day = _load(data_dir / rt.day_over_day_name)

        floor_simple = (
            ModelSourceDocument(
                node=FLOOR_SIMPLE_NODE,
                data_read="floor-simple.json numbers[merges]",
                read_at=str(simple[0].get("generated_at") or simple[1]),
                document=simple[0],
            )
            if simple
            else None
        )
        delegation = (
            ModelSourceDocument(
                node=DELEGATION_NODE,
                data_read="delegation-history.json models and series[task_kind=*].hours",
                read_at=str(history[0].get("generated_at") or history[1]),
                document=history[0],
            )
            if history
            else None
        )
        seen = self._keep_readings(
            data_dir / rt.readings_name, simple[0] if simple else None, now
        )
        report: ModelDayOverDayReport | None = None
        if day:
            try:
                report = ModelDayOverDayReport.model_validate(day[0])
            except ValueError:
                report = None

        panel = PanelFold().handle(
            ModelCostThroughputRequest(
                generated_at=now.strftime(_STAMP),
                floor_simple=floor_simple,
                delegation_history=delegation,
                seen_running_series=seen,
                day_over_day=report,
            )
        )
        target = data_dir / rt.output_name
        _write_atomic(target, panel.model_dump_json(indent=1).encode())
        return ModelFloorCostThroughputResult(
            ok=True,
            wrote=str(target),
            detail=f"{panel.unreadable_count} figures read UNREADABLE",
            unreadable=panel.unreadable_count,
        )

    def _keep_readings(
        self, path: Path, simple: dict[str, Any] | None, now: datetime
    ) -> ModelSeenRunningSeries | None:
        """Append the integration record's counts when its ``as_of`` is new, drop what is older than the keep window, return the series."""
        kept: list[ModelSeenRunningPoint] = []
        previous = _load(path)
        if previous:
            for raw in previous[0].get("points", []):
                try:
                    kept.append(ModelSeenRunningPoint.model_validate(raw))
                except ValueError:
                    continue
        integration = simple.get("integration") if simple else None
        counts = integration.get("counts") if isinstance(integration, dict) else None
        if (
            isinstance(integration, dict)
            and integration.get("status") == "read"
            and isinstance(counts, dict)
            and isinstance(counts.get("merged"), int)
            and isinstance(counts.get("seen_running"), int)
            and isinstance(integration.get("as_of"), str)
            and integration["as_of"] not in {p.read_at for p in kept}
        ):
            kept.append(
                ModelSeenRunningPoint(
                    read_at=integration["as_of"],
                    merged=counts["merged"],
                    seen_running=counts["seen_running"],
                )
            )
        cutoff = (now - timedelta(days=self._rt.readings_keep_days)).strftime(_STAMP)
        kept = sorted((p for p in kept if p.read_at >= cutoff), key=lambda p: p.read_at)
        if not kept:
            return None
        _write_atomic(
            path,
            json.dumps({"points": [p.model_dump() for p in kept]}, indent=1).encode(),
        )
        return ModelSeenRunningSeries(
            node=FLOOR_SIMPLE_NODE, data_read=SEEN_READ, points=tuple(kept)
        )
