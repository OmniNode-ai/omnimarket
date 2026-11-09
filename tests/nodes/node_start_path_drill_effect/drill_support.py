# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A test world for the start-path drill: the example overlay, in-memory ledgers and named injected breaks."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_ledger import (
    LedgerStartAdmissionLog,
)
from omnimarket.nodes.node_start_path_drill_effect.handlers.handler_start_path_drill import (
    HandlerStartPathDrill,
    in_memory_drill_store,
    load_declared_paths,
)
from omnimarket.nodes.node_start_path_drill_effect.overlay import (
    ModelStartPathDrillOverlay,
)

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_start_path_drill_effect/overlay.example.yaml"
)
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC)

#: The plan's nine paths and the six the second reader names, spelled here and not read from the contract, so a
#: path dropped from the contract fails the test that compares the two.
EXPECTED_PATHS = (
    "lane_runner",
    "brief_check",
    "lab_fill_a",
    "lab_fill_b",
    "lab_fill_c",
    "agent_workflow_hook",
    "landing_fix_workers",
    "scheduled_ticks",
    "cloud_lanes",
    "node_overnight",
    "node_demo_fanout_orchestrator",
    "node_coding_agent_invoke_effect",
    "harness_delegate",
    "epic_team_run_sh",
    "lab_run_agent_commands",
)


def example_overlay(**changes: object) -> ModelStartPathDrillOverlay:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document.update(changes)
    return ModelStartPathDrillOverlay.model_validate(document)


class AdmitsEverythingStore:
    """The named break of the gate: a store that ignores the stop and admits every start."""

    def __init__(self) -> None:
        self._n = 0

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        self._n += 1
        return ModelGateStoreResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=EnumGateStoreOutcome.OK,
            epoch=self._n,
        )


class RefusesEverythingStore:
    """The named break of the control: a store that refuses every start, so nothing is ever admitted."""

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        refused = request.operation is EnumGateStoreOperation.RESERVE
        return ModelGateStoreResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            outcome=EnumGateStoreOutcome.REFUSED_LIMIT
            if refused
            else EnumGateStoreOutcome.OK,
            epoch=1,
        )


class World:
    """The drill's inputs, with one place per named break."""

    def __init__(self, *, batch: bool = False) -> None:
        self.batch = batch
        self.overlay = example_overlay()
        self.declared = load_declared_paths()
        self.admission_paths: tuple[str, ...] = ()
        self.reading_ledger = InMemoryLedger()
        self.log_ledger = InMemoryLedger()
        self.store_factory: object = None

    def break_path_left_out_of_the_census(self, path: str) -> None:
        census = tuple(p for p in self.overlay.census if p.id != path)
        self.overlay = self.overlay.model_copy(update={"census": census})

    def break_admission_names_a_path_the_census_lacks(self, path: str) -> None:
        self.admission_paths = (*self.admission_paths, path)

    def break_gate_admits_everything(self) -> None:
        self.store_factory = AdmitsEverythingStore

    def break_gate_refuses_everything(self) -> None:
        self.store_factory = RefusesEverythingStore

    def break_observe_log_refuses_rows(self) -> None:
        self.log_ledger.append_status = "refused"

    def handler(self, *, write: bool = True) -> HandlerStartPathDrill:
        factory: object = self.store_factory
        store_factory = (
            in_memory_drill_store(self.overlay.gate) if factory is None else factory
        )
        log = LedgerStartAdmissionLog(
            self.log_ledger,
            lane=self.overlay.publish.observe_lane,
            actor=self.overlay.publish.actor,
            host="example-lab-host",
            clock=lambda: NOW,
            batch=self.batch,
        )
        return HandlerStartPathDrill(
            overlay=self.overlay,
            host="example-lab-host",
            declared_paths=self.declared,
            admission_paths=self.admission_paths,
            log=log,
            appender=self.reading_ledger if write else None,
            clock=lambda: NOW,
            store_factory=store_factory,  # type: ignore[arg-type]
        )
