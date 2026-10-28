# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``run_parity``: walk the contract's execution graph and compare the runs of every profile."""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from importlib.resources import files
from typing import Any, Literal

import yaml
from omnibase_core.models.contracts.model_contract_workflow_definition import (
    ModelContractWorkflowDefinition,
)

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_compare_runs import (
    compare_runs,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_scenario import (
    run_node_operation,
    run_stub_rows,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelCompareResult,
    ModelParityRequest,
    ModelParityResult,
    ModelScenarioRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_binding_parity_orchestrator.overlay import (
    OVERLAY_ID,
    ModelBindingProfile,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_registry import (
    BUS_FACTORIES,
    CLOCK_FACTORIES,
    PROTOCOL_BUS,
    PROTOCOL_CLOCK,
)

ProfileRole = Literal["baseline", "each_other"]


def _loop_is_running() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def run_sync[ResultT](coroutine: Coroutine[Any, Any, ResultT]) -> ResultT:
    """Run a coroutine to completion, on a worker thread when a loop is already running."""
    if not _loop_is_running():
        return asyncio.run(coroutine)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


@dataclass
class _WalkState:
    request: ModelParityRequest
    baseline: ModelBindingProfile | None = None
    others: tuple[ModelBindingProfile, ...] = ()
    binding_ids: dict[str, dict[str, str]] = field(default_factory=dict)
    runs: dict[str, ModelScenarioRun] = field(default_factory=dict)
    comparison: ModelCompareResult | None = None


def _ordered_nodes(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dependency order that keeps the contract's order among nodes that are ready together."""
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


class HandlerParityRun:
    """``handle(ModelParityRequest) -> ModelParityResult`` by walking the contract's graph."""

    def __init__(self) -> None:
        document = yaml.safe_load(
            files("omnimarket.nodes.node_binding_parity_orchestrator")
            .joinpath("contract.yaml")
            .read_text(encoding="utf-8")
        )
        workflow = document["workflow_coordination"]["workflow_definition"]
        # Structural validation: ids, edges and acyclicity. The model drops step_config, so the
        # operations are read from the raw mapping beside it.
        ModelContractWorkflowDefinition.model_validate(workflow)
        self._steps = _ordered_nodes(workflow["execution_graph"]["nodes"])
        self._run_local = ModelRunLocalConfig.model_validate(
            document["config"]["run_local"]
        )
        self._operations: dict[str, Callable[[_WalkState, dict[str, Any]], None]] = {
            "resolve_profile_bindings": self._resolve_profile_bindings,
            "run_scenario": self._run_scenario,
            "compare_runs": self._compare_runs,
        }

    def handle(self, request: ModelParityRequest) -> ModelParityResult:
        state = _WalkState(request=request)
        walked: list[str] = []
        for step in self._steps:
            step_config: dict[str, Any] = step.get("step_config") or {}
            operation = step_config.get("operation")
            if operation is not None:
                if operation not in self._operations:
                    raise ValueError(
                        f"execution_graph node {step['node_id']!r} names unknown "
                        f"operation {operation!r}"
                    )
                self._operations[operation](state, step_config)
            walked.append(step["node_id"])
        comparison = state.comparison
        if comparison is None:
            raise ValueError("execution_graph has no compare_runs step")
        return ModelParityResult(
            passed=comparison.passed,
            runs=tuple(state.runs.values()),
            comparison=comparison,
            first_difference=comparison.first_difference,
            steps=tuple(walked),
        )

    def _resolve_profile_bindings(
        self, state: _WalkState, step_config: dict[str, Any]
    ) -> None:
        overlay = state.request.overlay
        state.baseline, state.others = overlay.parity_profiles()
        for profile in (state.baseline, *state.others):
            ids: dict[str, str] = {}
            for code, factories in (
                (PROTOCOL_BUS, BUS_FACTORIES),
                (PROTOCOL_CLOCK, CLOCK_FACTORIES),
            ):
                binding_id = overlay.binding_id(profile.id, code)
                if binding_id not in factories:
                    raise NodeOverlayError(
                        f"{OVERLAY_ID}.bindings.{profile.id}.{code} names {binding_id!r}, "
                        f"which no stub binding provides; known: {sorted(factories)}"
                    )
                ids[code] = binding_id
            state.binding_ids[profile.id] = ids

    def _run_scenario(self, state: _WalkState, step_config: dict[str, Any]) -> None:
        role: ProfileRole = step_config["profile_role"]
        if state.baseline is None:
            raise ValueError("run_scenario ran before resolve_profile_bindings")
        profiles = (state.baseline,) if role == "baseline" else state.others
        for profile in profiles:
            state.runs[profile.id] = run_sync(self._run_profile(state, profile))

    async def _run_profile(
        self, state: _WalkState, profile: ModelBindingProfile
    ) -> ModelScenarioRun:
        request = state.request
        ids = state.binding_ids[profile.id]
        bus = BUS_FACTORIES[ids[PROTOCOL_BUS]]()
        clock = CLOCK_FACTORIES[ids[PROTOCOL_CLOCK]](request.fixed_now)
        proof_eligible = profile.id in request.overlay.proof_bindings
        if request.scenario.kind == "stub_rows":
            return await run_stub_rows(
                profile_id=profile.id,
                proof_eligible=proof_eligible,
                scenario=request.scenario,
                bus=bus,
                clock=clock,
            )
        return await run_node_operation(
            profile_id=profile.id,
            proof_eligible=proof_eligible,
            scenario=request.scenario,
            bus=bus,
            config=self._run_local,
        )

    def _compare_runs(self, state: _WalkState, step_config: dict[str, Any]) -> None:
        if state.baseline is None:
            raise ValueError("compare_runs ran before resolve_profile_bindings")
        ordered = tuple(state.runs[p.id] for p in (state.baseline, *state.others))
        state.comparison = compare_runs(
            ordered, state.request.scenario.allowed_differences
        )
