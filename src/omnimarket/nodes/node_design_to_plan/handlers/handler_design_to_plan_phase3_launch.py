"""HandlerDesignToPlanPhase3Launch - Phase 3 native launch routing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from omnimarket.events.design_to_plan import ModelPlanToTicketsStartCommand
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_command import (
    ModelDesignToPlanCommand,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_phase3_launch import (
    ModelDesignToPlanPhase3Dispatch,
    ModelDesignToPlanPhase3LaunchResult,
)
from omnimarket.nodes.node_design_to_plan.models.model_design_to_plan_state import (
    EnumDesignToPlanPhase,
    ModelDesignToPlanState,
)

_NODES_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_PLAN_TO_TICKETS_MODEL = (
    "omnimarket.events.design_to_plan.ModelPlanToTicketsStartCommand"
)


@dataclass(frozen=True)
class _Phase3Route:
    route_id: str
    target_node: str
    command_topic: str
    command_model: str


def _load_yaml(path: Path) -> dict[str, object]:
    if not path.exists():
        msg = f"contract.yaml not found at {path}"
        raise RuntimeError(msg)
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        msg = f"contract.yaml at {path} must parse to a mapping"
        raise RuntimeError(msg)
    return loaded


def _load_phase3_route() -> _Phase3Route:
    contract = _load_yaml(_CONTRACT_PATH)
    metadata = contract.get("metadata")
    if not isinstance(metadata, dict):
        msg = "node_design_to_plan contract metadata.phase3_launch is missing"
        raise RuntimeError(msg)
    phase3_launch = metadata.get("phase3_launch")
    if not isinstance(phase3_launch, dict):
        msg = "node_design_to_plan contract metadata.phase3_launch is missing"
        raise RuntimeError(msg)
    routes = phase3_launch.get("routes")
    if not isinstance(routes, list) or len(routes) != 1:
        msg = "node_design_to_plan Phase 3 must declare exactly one native route"
        raise RuntimeError(msg)
    route_data = routes[0]
    if not isinstance(route_data, dict):
        msg = "node_design_to_plan Phase 3 route must be a mapping"
        raise RuntimeError(msg)

    route = _Phase3Route(
        route_id=str(route_data.get("route_id") or ""),
        target_node=str(route_data.get("target_node") or ""),
        command_topic=str(route_data.get("command_topic") or ""),
        command_model=str(route_data.get("command_model") or ""),
    )
    if not all(
        (route.route_id, route.target_node, route.command_topic, route.command_model)
    ):
        msg = "node_design_to_plan Phase 3 route is incomplete"
        raise RuntimeError(msg)

    downstream = _load_yaml(_NODES_ROOT / route.target_node / "contract.yaml")
    handler = downstream.get("handler")
    event_bus = downstream.get("event_bus")
    if not isinstance(handler, dict) or not isinstance(event_bus, dict):
        msg = f"{route.target_node} contract must declare handler and event_bus"
        raise RuntimeError(msg)
    downstream_model = str(handler.get("input_model") or "")
    subscribe_topics = event_bus.get("subscribe_topics")
    if downstream_model != route.command_model:
        msg = (
            "node_design_to_plan Phase 3 command_model does not match "
            f"{route.target_node} handler.input_model"
        )
        raise RuntimeError(msg)
    if (
        not isinstance(subscribe_topics, list)
        or route.command_topic not in subscribe_topics
    ):
        msg = (
            "node_design_to_plan Phase 3 command_topic is not subscribed by "
            f"{route.target_node}"
        )
        raise RuntimeError(msg)
    if route.command_model != _PLAN_TO_TICKETS_MODEL:
        msg = "node_design_to_plan Phase 3 currently supports plan_to_tickets only"
        raise RuntimeError(msg)
    return route


class HandlerDesignToPlanPhase3Launch:
    """Build typed Onex-native launch commands for a finalized plan."""

    def handle(
        self,
        command: ModelDesignToPlanCommand,
        state: ModelDesignToPlanState,
    ) -> ModelDesignToPlanPhase3LaunchResult:
        """Build the Phase 3 downstream dispatch plan from contract truth."""
        if state.current_phase != EnumDesignToPlanPhase.LAUNCH:
            msg = (
                "Phase 3 launch routing requires state.current_phase='launch' "
                f"(got {state.current_phase.value!r})"
            )
            raise ValueError(msg)

        plan_path = state.plan_path or command.plan_path
        dry_run = command.dry_run or state.dry_run
        plan_only = command.plan_only or state.plan_only
        if command.no_launch or state.no_launch:
            return ModelDesignToPlanPhase3LaunchResult(
                correlation_id=command.correlation_id,
                status="skipped",
                plan_path=str(plan_path) if plan_path else None,
                dry_run=dry_run,
                plan_only=plan_only,
                dispatches=(),
            )
        if not plan_path:
            msg = "Phase 3 launch routing requires a finalized plan_path"
            raise ValueError(msg)

        route = _load_phase3_route()
        downstream_command = ModelPlanToTicketsStartCommand(
            correlation_id=str(command.correlation_id),
            plan_path=str(plan_path),
            epic_title=command.topic,
            dry_run=dry_run or plan_only,
            team="Omninode",
            repo="omnimarket",
        )
        dispatch = ModelDesignToPlanPhase3Dispatch(
            route_id=route.route_id,
            target_node=route.target_node,
            command_topic=route.command_topic,
            command_model=route.command_model,
            command=downstream_command,
        )

        return ModelDesignToPlanPhase3LaunchResult(
            correlation_id=command.correlation_id,
            status="planned" if dry_run or plan_only else "ready",
            plan_path=str(plan_path),
            dry_run=dry_run,
            plan_only=plan_only,
            dispatches=(dispatch,),
        )


__all__: list[str] = ["HandlerDesignToPlanPhase3Launch"]
