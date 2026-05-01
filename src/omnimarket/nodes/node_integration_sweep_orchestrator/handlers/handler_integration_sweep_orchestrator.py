import os
from datetime import date
from pathlib import Path

import yaml

from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_result import (
    ModelIntegrationSweepOrchestratorResult,
)


class HandlerIntegrationSweepOrchestrator:
    """Write deterministic integration sweep artifacts."""

    def handle(
        self, request: ModelIntegrationSweepOrchestratorRequest
    ) -> ModelIntegrationSweepOrchestratorResult:
        artifact_root = self._resolve_root(request.artifact_root)
        artifact_date = request.artifact_date or date.today().isoformat()
        artifact_path = (
            artifact_root / "drift" / "integration" / f"{artifact_date}.yaml"
        )
        tickets = [
            ticket.strip().upper() for ticket in request.tickets if ticket.strip()
        ]

        payload = {
            "artifact_type": "ModelIntegrationRecord",
            "artifact_version": "1.0.0",
            "date": artifact_date,
            "scope": request.scope or "explicit",
            "status": "recorded",
            "tickets": tickets,
            "surfaces": [],
        }

        artifact_written = False
        if not request.dry_run:
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(
                yaml.safe_dump(payload, sort_keys=True),
                encoding="utf-8",
            )
            artifact_written = True

        return ModelIntegrationSweepOrchestratorResult(
            status="recorded",
            artifact_path=str(artifact_path),
            artifact_written=artifact_written,
            ticket_count=len(tickets),
            details={
                "dry_run": str(request.dry_run).lower(),
                "artifact_date": artifact_date,
            },
        )

    @staticmethod
    def _resolve_root(configured: str) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        env_root = os.environ.get("ONEX_CC_REPO_PATH")
        if env_root:
            return Path(env_root).expanduser().resolve()
        return Path.cwd().resolve()
