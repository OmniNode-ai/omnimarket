# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``contract_seams_run``: walk the contract's execution graph over one design (OMN-21627).

Each graph step names its operation, the existing node it runs and the reducer trigger it
folds. The nodes are run through a runner bound to the injected bus: on the in-memory
overlay that runner resolves each node's handler from the node's own contract and records
its command and terminal event on the bus. The run's state lives in
node_contract_seams_reducer and is read back after every fold; a refused or failed state
stops the walk.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.contracts.model_contract_workflow_definition import (
    ModelContractWorkflowDefinition,
)
from omnibase_core.models.validation.model_contract_walk_report import (
    ModelContractWalkReport,
)
from pydantic import JsonValue

from omnimarket.models.contract_seams_design import ModelContractSeamsDesign
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_contract_steps import (
    HandlerContractNodeStep,
    ModelDrivenContractChain,
    drive_contract_chain,
)
from omnimarket.nodes.node_contract_seams_orchestrator.handlers.handler_emit_contracts import (
    ModelEmitConfig,
    ModelEmittedContract,
    emit_contracts,
)
from omnimarket.nodes.node_contract_seams_orchestrator.models.model_contract_seams_run import (
    ModelContractSeamsConfig,
    ModelContractSeamsRunRequest,
    ModelContractSeamsRunResult,
    ModelContractSeamsSeamVerdict,
    ModelContractSeamsStepTransition,
)
from omnimarket.nodes.node_contract_seams_orchestrator.protocols.protocol_contract_seams_bus import (
    ProtocolContractSeamsBus,
)
from omnimarket.protocols.protocol_clock import ProtocolClock

_PACKAGE = "omnimarket.nodes.node_contract_seams_orchestrator"
_STOP_STATES = frozenset({"refused", "failed"})
_STEP_FAILED = "step_failed"
_MISMATCH = "MISMATCH"
_UNMATCHED = "UNMATCHED"
_SWEEP_PASS = "pass"


class _SystemClock:
    """The default clock binding: the host's wall clock in UTC."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def _run_sync[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Run a coroutine to completion, on a worker thread when a loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


def _ordered_steps(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dependency order that keeps the contract's order among steps ready together."""
    done: set[str] = set()
    ordered: list[dict[str, Any]] = []
    pending = list(raw_nodes)
    while pending:
        ready = [n for n in pending if set(n.get("depends_on", [])) <= done]
        if not ready:
            raise ValueError("execution_graph has a cycle or a dangling dependency")
        chosen = ready[0]
        ordered.append(chosen)
        done.add(chosen["node_id"])
        pending.remove(chosen)
    return ordered


def _load_object(reference: str) -> object:
    module_name, _, attribute = reference.partition(":")
    if not attribute:
        raise ValueError(f"{reference!r} must be 'module:attribute'")
    return cast(object, getattr(importlib.import_module(module_name), attribute))


class _RunRefusedError(Exception):
    """The reducer moved the run to a stop state; the walk ends here."""


@dataclass
class _RunState:
    request: ModelContractSeamsRunRequest
    bus: ProtocolContractSeamsBus
    runner: HandlerContractNodeStep
    fsm_state: str | None = None
    walked: list[str] = field(default_factory=list)
    transitions: list[ModelContractSeamsStepTransition] = field(default_factory=list)
    contracts: tuple[ModelEmittedContract, ...] = ()
    design_issues: tuple[str, ...] = ()
    scaffolded: list[str] = field(default_factory=list)
    seam_graph: dict[str, JsonValue] = field(default_factory=dict)
    seam_verdicts: list[ModelContractSeamsSeamVerdict] = field(default_factory=list)
    walk_issues: tuple[str, ...] = ()
    drive: ModelDrivenContractChain | None = None
    undriven: tuple[str, ...] = ()
    sweep_status: str | None = None
    refused_by: str | None = None
    failure: str | None = None

    @property
    def design(self) -> ModelContractSeamsDesign:
        return self.request.design

    @property
    def output_root(self) -> Path:
        return Path(self.request.output_root)


StepOperation = Callable[
    [_RunState, dict[str, Any], str], Coroutine[Any, Any, dict[str, JsonValue] | None]
]


class HandlerContractSeamsRun:
    """``handle(ModelContractSeamsRunRequest) -> ModelContractSeamsRunResult``."""

    def __init__(
        self,
        event_bus: ProtocolContractSeamsBus | None = None,
        clock: ProtocolClock | None = None,
    ) -> None:
        document = yaml.safe_load(
            files(_PACKAGE).joinpath("contract.yaml").read_text(encoding="utf-8")
        )
        workflow = document["workflow_coordination"]["workflow_definition"]
        # Structural validation of ids, edges and acyclicity; step_config is read beside it.
        ModelContractWorkflowDefinition.model_validate(workflow)
        self._steps = _ordered_steps(workflow["execution_graph"]["nodes"])
        self._config = ModelContractSeamsConfig.model_validate(
            document["config"]["contract_seams"]
        )
        # The in-memory overlay's bindings are injected defaults, never hidden fallbacks.
        self._bus = event_bus
        self._clock: ProtocolClock = clock if clock is not None else _SystemClock()
        self._operations: dict[str, StepOperation] = {
            "emit_contracts": self._emit_contracts,
            "scaffold_nodes": self._scaffold_nodes,
            "read_seam_graph": self._read_seam_graph,
            "match_seams": self._match_seams,
            "walk_event_chain": self._walk_event_chain,
            "gate_chains": self._gate_chains,
            "sweep_golden_chain": self._sweep_golden_chain,
            "confirm_connected": self._confirm_connected,
        }
        for step in self._steps:
            operation = (step.get("step_config") or {}).get("operation")
            if operation not in self._operations:
                raise ValueError(
                    f"execution_graph node {step['node_id']!r} names unknown "
                    f"operation {operation!r}"
                )

    def handle(
        self, request: ModelContractSeamsRunRequest
    ) -> ModelContractSeamsRunResult:
        return _run_sync(self._run(request))

    async def _run(
        self, request: ModelContractSeamsRunRequest
    ) -> ModelContractSeamsRunResult:
        owns_bus = self._bus is None
        bus: ProtocolContractSeamsBus = (
            self._bus
            if self._bus is not None
            else EventBusInmemory(
                environment="contract-seams", group=self._config.drive_consumer_group
            )
        )
        await bus.start()
        state = _RunState(request=request, bus=bus, runner=HandlerContractNodeStep(bus))
        try:
            for step in self._steps:
                node_id: str = step["node_id"]
                step_config: dict[str, Any] = step.get("step_config") or {}
                operation = self._operations[step_config["operation"]]
                try:
                    facts = await operation(state, step_config, node_id)
                    state.walked.append(node_id)
                    trigger = step_config.get("trigger")
                    if trigger is not None:
                        await self._fold(state, node_id, trigger, facts or {})
                except _RunRefusedError:
                    break
                except Exception as raised:
                    state.failure = f"{node_id}: {type(raised).__name__}: {raised}"
                    await self._fold(state, node_id, _STEP_FAILED, {}, stop=False)
                    break
        finally:
            if owns_bus:
                await bus.close()
        return ModelContractSeamsRunResult(
            correlation_id=request.correlation_id,
            design_id=request.design.design_id,
            final_state=state.fsm_state or "",
            steps=tuple(state.walked),
            transitions=tuple(state.transitions),
            design_issues=state.design_issues,
            scaffolded_nodes=tuple(state.scaffolded),
            seam_verdicts=tuple(state.seam_verdicts),
            walk_issues=state.walk_issues,
            chain_event_types=state.drive.event_types if state.drive else (),
            chain_states=state.drive.states if state.drive else (),
            undriven_path_ids=state.undriven,
            sweep_status=state.sweep_status,
            refused_by=state.refused_by,
            failure=state.failure,
        )

    async def _fold(
        self,
        state: _RunState,
        node_id: str,
        trigger: str,
        facts: dict[str, JsonValue],
        *,
        stop: bool = True,
    ) -> None:
        payload: dict[str, JsonValue] = {
            "state": state.fsm_state,
            "trigger": trigger,
            "facts": facts,
            "now": self._clock.now().isoformat(),
        }
        result = await state.runner.run(
            self._config.reducer_node, self._config.reducer_operation, payload
        )
        failed = result.get("failed_conditions")
        record = ModelContractSeamsStepTransition(
            step=node_id,
            trigger=trigger,
            prior_state=str(result["prior_state"]),
            state=str(result["state"]),
            moved=bool(result["moved"]),
            failed_conditions=tuple(str(c) for c in failed)
            if isinstance(failed, list)
            else (),
        )
        state.transitions.append(record)
        state.fsm_state = record.state
        if not record.moved:
            raise RuntimeError(
                f"reducer held the run at {record.prior_state} on {trigger}: "
                f"{result.get('error')}"
            )
        if stop and record.state in _STOP_STATES:
            state.refused_by = node_id
            raise _RunRefusedError(node_id)

    # -- steps -------------------------------------------------------------------------

    async def _emit_contracts(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        config = self._config
        emitted = emit_contracts(
            state.design,
            ModelEmitConfig(
                handler_module_template=config.handler_module_template,
                handler_class_template=config.handler_class_template,
                input_model_template=config.input_model_template,
                emitted_version=config.emitted_version,
                emitted_lifecycle=config.emitted_lifecycle,
                generated_by=_PACKAGE.rpartition(".")[2],
            ),
        )
        state.contracts = emitted.contracts
        state.design_issues = emitted.design_issues
        return {
            "contract_count": len(emitted.contracts),
            "design_issue_count": len(emitted.design_issues),
        }

    async def _scaffold_nodes(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        archetypes = {node.node_name: node.archetype for node in state.design.nodes}
        for contract in state.contracts:
            result = await state.runner.run(
                step_config["node"],
                step_config["node_operation"],
                {
                    "correlation_id": str(state.request.correlation_id),
                    "node_name": contract.node_name,
                    "node_type": archetypes[contract.node_name].value,
                    "output_dir": str(state.output_root / contract.node_name),
                    "contract_yaml": contract.contract_yaml,
                },
            )
            created = result.get("created_files")
            written = state.output_root / contract.node_name / "contract.yaml"
            if (
                isinstance(created, list)
                and "contract.yaml" in created
                and written.read_text(encoding="utf-8") == contract.contract_yaml
            ):
                state.scaffolded.append(contract.node_name)
        return {"unscaffolded_count": len(state.contracts) - len(state.scaffolded)}

    async def _read_seam_graph(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> None:
        state.seam_graph = await state.runner.run(
            step_config["node"],
            step_config["node_operation"],
            {
                "repo_base_path": str(state.output_root),
                "discovery_roots": list(state.scaffolded),
            },
        )
        return

    async def _match_seams(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        raw_edges = state.seam_graph.get("edges")
        edges = (
            [e for e in raw_edges if isinstance(e, dict)]
            if isinstance(raw_edges, list)
            else []
        )
        for crossing in state.design.crossings:
            sides: dict[str, dict[str, JsonValue] | None] = {}
            for role in ("producer", "consumer"):
                declared = [
                    edge
                    for edge in edges
                    if edge.get("edge_id") == crossing.crossing_id
                    and edge.get("role") == role
                    and edge.get("envelope_model")
                    and edge.get("envelope_version")
                ]
                sides[role] = (
                    {
                        "edge_id": crossing.crossing_id,
                        "role": role,
                        "topic": declared[0]["topic"],
                        "envelope_model": declared[0]["envelope_model"],
                        "envelope_version": declared[0]["envelope_version"],
                        "key_fields": declared[0].get("key_fields") or [],
                        "delivery_semantics": declared[0].get("delivery_semantics"),
                    }
                    if len(declared) == 1
                    else None
                )
            verdict = await state.runner.run(
                step_config["node"],
                step_config["node_operation"],
                {
                    "edge_id": crossing.crossing_id,
                    "declared_producer": sides["producer"],
                    "declared_consumer": sides["consumer"],
                },
            )
            leg1 = verdict.get("leg1_declared_vs_declared")
            path = (
                leg1.get("mismatching_field_path") if isinstance(leg1, dict) else None
            )
            state.seam_verdicts.append(
                ModelContractSeamsSeamVerdict(
                    crossing_id=crossing.crossing_id,
                    verdict=str(verdict["verdict"]),
                    mismatching_field_path=path if isinstance(path, str) else None,
                )
            )
        verdicts = [v.verdict for v in state.seam_verdicts]
        return {
            "seam_count": len(verdicts),
            "seam_mismatch_count": verdicts.count(_MISMATCH),
            "seam_unmatched_count": verdicts.count(_UNMATCHED),
        }

    def _scaffolded_contracts(self, state: _RunState) -> dict[str, Path]:
        return {
            name: state.output_root / name / "contract.yaml"
            for name in state.scaffolded
        }

    async def _walk_event_chain(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> None:
        paths = self._scaffolded_contracts(state)
        walker = cast(
            "Callable[[list[Path]], ModelContractWalkReport]",
            _load_object(self._config.contract_walker),
        )
        report = walker([path.parent for path in sorted(paths.values())])
        issues: list[str] = [
            f"{entry.node}: not armed: {entry.reason}" for entry in report.not_armed
        ]
        for workflow in report.workflows:
            owner = workflow.workflow_owner
            issues.extend(
                f"{owner}: unreachable state {ref.node}.{ref.state}"
                for ref in workflow.unreachable_states
            )
            issues.extend(
                f"{owner}: dead state {', '.join(states)}"
                for states in workflow.dead_states
            )
            issues.extend(
                f"{owner}: no golden exit from {', '.join(states)}"
                for states in workflow.no_golden_exit_states
            )
            if workflow.truncated:
                issues.append(f"{owner}: product graph truncated")
        state.walk_issues = tuple(issues)
        documents = {
            name: cast(
                "dict[str, object]", yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            for name, path in paths.items()
        }
        state.drive = await asyncio.wait_for(
            drive_contract_chain(
                state.bus,
                documents,
                head_topic=state.design.head.topic,
                tail_topic=state.design.tail.topic,
                correlation_id=str(state.request.correlation_id),
                group_prefix=self._config.drive_consumer_group,
            ),
            timeout=self._config.drive_timeout_seconds,
        )
        return

    async def _gate_chains(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        design = state.design
        steps: list[tuple[str, str, str]] = [
            ("head", design.head.topic, design.head.node_name),
            *((c.producer, c.topic, c.consumer) for c in design.crossings),
            (design.tail.node_name, design.tail.topic, "tail"),
        ]
        path_id = "golden:" + ">".join(topic for _, topic, _ in steps)
        drive = state.drive
        missing = (
            sorted({topic for _, topic, _ in steps} - set(drive.event_types))
            if drive is not None
            else [topic for _, topic, _ in steps]
        )
        chains: list[JsonValue] = []
        undriven: dict[str, JsonValue] = {}
        if drive is not None and drive.delivered and not missing:
            chains.append(
                {
                    "path_id": path_id,
                    "kind": "golden",
                    "expected_event_types": list(drive.event_types),
                    "expected_states": list(drive.states),
                    "terminal_fields": {},
                }
            )
        else:
            undriven[path_id] = "not delivered on the bus: " + ", ".join(missing)
        generated: dict[str, JsonValue] = {
            "workflow_owner": design.design_id,
            "chains": chains,
            "undriven": undriven,
        }
        # A first run has no committed chain set: it records the drive and the gate
        # reads the paths it could not drive. A committed set compares a later run.
        verdict = await state.runner.run(
            step_config["node"],
            step_config["node_operation"],
            {
                "workflow_owner": design.design_id,
                "obligations": [
                    {
                        "path_id": path_id,
                        "kind": "golden",
                        "steps": [list(s) for s in steps],
                    }
                ],
                "committed": generated,
                "generated": generated,
            },
        )
        raw_undriven = verdict.get("undriven_path_ids")
        state.undriven = (
            tuple(str(p) for p in raw_undriven)
            if isinstance(raw_undriven, list)
            else ()
        )
        return {
            "walk_issue_count": len(state.walk_issues),
            "chain_count": len(chains),
            "undriven_path_count": len(state.undriven),
        }

    async def _sweep_golden_chain(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        design = state.design
        drive = state.drive
        rows: dict[str, JsonValue] = (
            {design.design_id: drive.tail_payload}
            if drive is not None and drive.tail_payload is not None
            else {}
        )
        result = await state.runner.run(
            step_config["node"],
            step_config["node_operation"],
            {
                "correlation_id": str(state.request.correlation_id),
                "chains": [
                    {
                        "name": design.design_id,
                        "head_topic": design.head.topic,
                        "tail_table": design.tail.topic,
                        "expected_fields": list(
                            self._config.golden_chain_expected_fields
                        ),
                        "correlation_id": str(state.request.correlation_id),
                    }
                ],
                "projected_rows": rows,
                "census": {
                    "source": self._config.golden_chain_census_source,
                    "scanned_count": 1,
                },
            },
        )
        state.sweep_status = str(result.get("overall_status"))
        return {"sweep_passed": state.sweep_status == _SWEEP_PASS}

    async def _confirm_connected(
        self, state: _RunState, step_config: dict[str, Any], node_id: str
    ) -> dict[str, JsonValue]:
        return {"tail_delivered": state.drive is not None and state.drive.delivered}


__all__ = ["HandlerContractSeamsRun"]
