import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_request import (
    ModelDodSweepOrchestratorRequest,
)
from omnimarket.nodes.node_dod_sweep_orchestrator.models.model_dod_sweep_orchestrator_result import (
    ModelDodSweepOrchestratorResult,
)

_TICKET_RE = re.compile(r"^OMN-\d+$", re.IGNORECASE)


class HandlerDodSweepOrchestrator:
    """Targeted DoD sweep receipt writer."""

    def handle(
        self, request: ModelDodSweepOrchestratorRequest
    ) -> ModelDodSweepOrchestratorResult:
        ticket_id = request.scope.strip().upper()
        if not _TICKET_RE.match(ticket_id):
            return ModelDodSweepOrchestratorResult(
                status="skipped",
                skipped=1,
                details={
                    "reason": "targeted_ticket_scope_required",
                    "scope": request.scope,
                },
            )

        contract_root = self._resolve_root(request.contract_root)
        evidence_root = self._resolve_root(request.evidence_root)
        contract_path = contract_root / "contracts" / f"{ticket_id}.yaml"
        receipt_path = evidence_root / ".evidence" / ticket_id / "dod_report.json"
        contract_exists = contract_path.is_file()
        failed = 0 if contract_exists else 1
        status = "verified" if contract_exists else "missing_contract"

        payload = {
            "ticket_id": ticket_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "result": {
                "status": status,
                "failed": failed,
                "skipped": 0,
                "contract_exists": contract_exists,
            },
            "checks": [
                {
                    "id": "contract_exists",
                    "status": "pass" if contract_exists else "fail",
                    "path": str(contract_path),
                }
            ],
        }

        receipt_written = False
        if not request.dry_run:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            receipt_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            receipt_written = True

        return ModelDodSweepOrchestratorResult(
            status=status,
            ticket_id=ticket_id,
            receipt_path=str(receipt_path),
            receipt_written=receipt_written,
            contract_path=str(contract_path),
            contract_exists=contract_exists,
            failed=failed,
            skipped=0,
            details={"mode": "targeted", "dry_run": str(request.dry_run).lower()},
        )

    @staticmethod
    def _resolve_root(configured: str) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        env_root = os.environ.get("ONEX_CC_REPO_PATH")  # contract-config-ok: config  # fmt: skip
        if env_root:
            return Path(env_root).expanduser().resolve()
        return Path.cwd().resolve()
