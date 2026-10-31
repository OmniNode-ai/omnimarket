# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the factory floor tests: small, explicit facts, no I/O beyond the node's own contract."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_factory_floor_compute.models.model_factory_floor_input import (
    ModelFactoryFloorConfig,
    ModelFactoryFloorRequest,
    ModelFloorLedgerRow,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorMerge,
    ModelFloorPlanInput,
    ModelFloorPr,
    ModelFloorSelection,
    ModelFloorTableTask,
    ModelFloorWatcher,
    PrState,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)
PLAN_PATH = "beta/plans/2026-10-31-wave-plan.md"


def config() -> ModelFactoryFloorConfig:
    """The vocabularies exactly as the contract declares them."""
    raw = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return ModelFactoryFloorConfig.model_validate(raw["config"])


def ago(minutes: float) -> datetime:
    return NOW - timedelta(minutes=minutes)


def task(
    task_id: str,
    wave: str,
    *,
    kind: str = "lane",
    depends_on: tuple[str, ...] = (),
    tickets: tuple[str, ...] = (),
    prs: tuple[str, ...] = (),
) -> ModelFloorManifestTask:
    return ModelFloorManifestTask(
        id=task_id,
        title=f"{task_id} title",
        wave=wave,
        kind=kind,
        depends_on=depends_on,
        tickets=tickets,
        prs=prs,
    )


def manifest_of(plan: ModelFloorPlanInput) -> ModelFloorManifest:
    assert plan.manifest is not None
    return plan.manifest


def wave_plan(path: str = PLAN_PATH) -> ModelFloorPlanInput:
    """Wave 0 (two lanes and a gate), then wave 1a (one adopted task and its gate)."""
    return ModelFloorPlanInput(
        plan_path=path,
        title="Wave plan",
        tickets=("OMN-100",),
        manifest=ModelFloorManifest(
            title="Orchestration alignment, the wave plan (tasks)",
            epic="OMN-100",
            tasks=(
                task("W0.1", "0"),
                task("W0.2", "0"),
                task("G0", "0", kind="gate", depends_on=("W0.1", "W0.2")),
                task("P1", "1a", depends_on=("G0",)),
                task("G1a", "1a", kind="gate", depends_on=("P1", "G0")),
            ),
        ),
    )


def table_plan(path: str = "beta/plans/2026-10-01-old-plan.md") -> ModelFloorPlanInput:
    """A plan with no manifest: only the plans-today task table."""
    return ModelFloorPlanInput(
        plan_path=path,
        title="Old plan",
        tickets=("OMN-7", "OMN-8", "OMN-9"),
        table_tasks=(
            ModelFloorTableTask(
                label="T1", text="first", tickets=("OMN-7",), state="merged"
            ),
            ModelFloorTableTask(
                label="T2",
                text="second",
                tickets=("OMN-8",),
                prs=("repo#2",),
                state="open-red",
                state_basis="CI Summary red",
            ),
            ModelFloorTableTask(
                label="T3", text="third", tickets=("OMN-9",), state="not-started"
            ),
        ),
    )


def pr(
    key: str,
    state: PrState = "OPEN",
    *,
    title: str = "",
    head_ref: str = "",
    draft: bool = False,
    red: bool = False,
    tickets: tuple[str, ...] = (),
) -> ModelFloorPr:
    return ModelFloorPr(
        key=key,
        state=state,
        title=title,
        head_ref=head_ref,
        draft=draft,
        red=red,
        tickets=tickets,
    )


def merge(
    key: str, minutes_ago: float, *, title: str = "", tickets: tuple[str, ...] = ()
) -> ModelFloorMerge:
    return ModelFloorMerge(
        key=key, merged_at=ago(minutes_ago), title=title, tickets=tickets
    )


def row(
    kind: str, minutes_ago: float, lane: str = "lane-a", **cells: Any
) -> ModelFloorLedgerRow:
    return ModelFloorLedgerRow(ts=ago(minutes_ago), kind=kind, lane=lane, **cells)


def advertisement(
    host: str, *, running: int, slots: int, age_seconds: int = 5, cadence: int = 10
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=8,
        load1=0.5,
        mem_available_bytes=8 * 1024**3,
        running_units=running,
        max_units=slots,
        advertised_at=NOW - timedelta(seconds=age_seconds),
        cadence_seconds=cadence,
    )


def request(
    *,
    plans: tuple[ModelFloorPlanInput, ...] | None = None,
    prs: tuple[ModelFloorPr, ...] = (),
    merges: tuple[ModelFloorMerge, ...] = (),
    ledger: tuple[ModelFloorLedgerRow, ...] = (),
    selection: tuple[str, ...] = (),
    cfg: ModelFactoryFloorConfig | None = None,
    **extra: Any,
) -> ModelFactoryFloorRequest:
    return ModelFactoryFloorRequest(
        generated_at=NOW,
        config=cfg or config(),
        selection=ModelFloorSelection(plan_path_contains=selection),
        plans=plans if plans is not None else (wave_plan(),),
        watcher=ModelFloorWatcher(last_tick=ago(3), prs=prs, merges=merges),
        ledger=ledger,
        **extra,
    )
