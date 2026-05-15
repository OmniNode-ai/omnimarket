# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_dependency_health_sweep.

Chains: GraphifyRunner → ContractTopologyParser → CrossReferenceEngine →
        BaselineDiffEngine → event bus emit (if wired).

Topic strings are loaded from contract.yaml at module load time — no bare
topic literals in handler code per the contract-first policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_dependency_health_sweep.engine.baseline_diff import (
    BaselineDiffEngine,
)
from omnimarket.nodes.node_dependency_health_sweep.engine.contract_topology import (
    ContractTopologyParser,
)
from omnimarket.nodes.node_dependency_health_sweep.engine.cross_reference import (
    CrossReferenceEngine,
)
from omnimarket.nodes.node_dependency_health_sweep.engine.graphify_adapter import (
    GraphifyAdapter,
)
from omnimarket.nodes.node_dependency_health_sweep.engine.graphify_runner import (
    GraphifyRunner,
)
from omnimarket.nodes.node_dependency_health_sweep.models import (
    ModelDepHealthSweepCompletedEvent,
    ModelDepHealthSweepRequest,
    ModelDepHealthSweepResult,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)


def _load_completed_topic() -> str:
    """Load the sweep-completed publish topic from this node's contract.yaml."""
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    try:
        with open(contract_path) as f:
            data: dict[str, Any] = yaml.safe_load(f)
        topics: list[str] = data.get("event_bus", {}).get("publish_topics", [])
        return next((t for t in topics if "sweep-completed" in t), "")
    except Exception:
        return ""


_SWEEP_COMPLETED_TOPIC = _load_completed_topic()


class HandlerDepHealthSweep:
    """Analyze dependency health across the ONEX delegation pipeline repos.

    Accepts an optional event_bus for emitting sweep-completed telemetry.
    Engine chain: GraphifyRunner → ContractTopologyParser → CrossReferenceEngine
                  → BaselineDiffEngine → (event emit if bus wired)
    """

    def __init__(
        self,
        event_bus: ProtocolEventBusPublisher | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._graphify_adapter = GraphifyAdapter()
        self._graphify_runner = GraphifyRunner(adapter=self._graphify_adapter)
        self._topology_parser = ContractTopologyParser()
        self._cross_ref_engine = CrossReferenceEngine()
        self._baseline_engine = BaselineDiffEngine()

    def handle(self, request: ModelDepHealthSweepRequest) -> ModelDepHealthSweepResult:
        """Run the dependency health sweep and return structured findings."""
        run_id = request.run_id or str(uuid4())

        # Probe graphify to record which version produced findings
        probe = self._graphify_adapter.probe()
        graphify_version = probe.version if probe.available else "ast-fallback"

        # Collect handler paths from contract.yaml files in repo roots
        # (used by CrossReferenceEngine for contract-aware untested handler detection)
        contract_handler_paths = self._collect_contract_handler_paths(
            request.repo_roots
        )

        all_findings = []
        for repo_root_str in request.repo_roots:
            repo_root = Path(repo_root_str)
            if not repo_root.exists():
                logger.warning("Repo root does not exist: %s — skipping", repo_root)
                continue

            repo_label = repo_root.name

            import_graph = self._graphify_runner.run(root=repo_root)
            topology = self._topology_parser.parse(search_roots=[repo_root])

            # Cross-reference: filter contract_handler_paths to this repo
            repo_handler_paths = [
                p for p in contract_handler_paths if p.startswith(str(repo_root))
            ]
            findings = self._cross_ref_engine.analyze(
                import_graph=import_graph,
                topology=topology,
                repo_label=repo_label,
                repo_root=repo_root,
                contract_handler_paths=repo_handler_paths,
            )
            all_findings.extend(findings)

        # Compute summary by finding type
        summary: dict[str, int] = {}
        for finding in all_findings:
            key = finding.finding_type.value
            summary[key] = summary.get(key, 0) + 1

        # Baseline diff
        baseline_delta: int | None = None
        if request.baseline_path is not None:
            diff_result = self._baseline_engine.diff(
                current=all_findings,
                baseline_path=Path(request.baseline_path),
                current_graphify_version=graphify_version,
            )
            if diff_result is not None:
                baseline_delta = diff_result.delta

        status = "findings" if all_findings else "clean"

        result = ModelDepHealthSweepResult(
            status=status,
            run_id=run_id,
            findings=all_findings,
            summary=summary,
            baseline_delta=baseline_delta,
            graphify_version=graphify_version,
        )

        # Publish completed event if event bus is wired
        if self._event_bus is not None and _SWEEP_COMPLETED_TOPIC:
            self._emit_completed_event(run_id=run_id, result=result)

        return result

    def _collect_contract_handler_paths(self, repo_roots: list[str]) -> list[str]:
        """Collect handler_path values declared in contract.yaml files across all roots."""
        handler_paths: list[str] = []
        for root_str in repo_roots:
            root = Path(root_str)
            if not root.exists():
                continue
            for contract_path in root.rglob("contract.yaml"):
                try:
                    with open(contract_path) as f:
                        data: dict[str, Any] = yaml.safe_load(f)
                    if not isinstance(data, dict):
                        continue
                    handler = data.get("handler") or {}
                    module = handler.get("module", "")
                    if module:
                        # Convert module path to file path relative to root
                        module_path = module.replace(".", "/") + ".py"
                        handler_paths.append(str(root / "src" / module_path))
                except Exception:
                    continue
        return handler_paths

    def _emit_completed_event(
        self, run_id: str, result: ModelDepHealthSweepResult
    ) -> None:
        """Publish ModelDepHealthSweepCompletedEvent to the event bus (sync wrapper)."""
        event = ModelDepHealthSweepCompletedEvent(
            run_id=run_id,
            findings=list(result.findings),
            summary=dict(result.summary),
            captured_at=datetime.now(UTC),
        )
        payload = json.dumps(event.model_dump(mode="json"), default=str).encode("utf-8")
        try:
            asyncio.run(
                self._event_bus.publish(  # type: ignore[union-attr]
                    topic=_SWEEP_COMPLETED_TOPIC,
                    key=run_id.encode(),
                    value=payload,
                )
            )
        except Exception:
            logger.warning("Failed to emit sweep-completed event — bus publish error")
