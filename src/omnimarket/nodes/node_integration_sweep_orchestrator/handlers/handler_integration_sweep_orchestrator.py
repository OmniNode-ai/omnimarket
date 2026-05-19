import os
from datetime import date
from pathlib import Path

import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

from omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify import (
    HandlerRuntimeShaVerify,
    ModelRuntimeShaVerifyRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_result import (
    ModelIntegrationSweepOrchestratorResult,
)


class HandlerIntegrationSweepOrchestrator:
    """Write deterministic integration sweep artifacts."""

    def __init__(
        self, runtime_sha_handler: HandlerRuntimeShaVerify | None = None
    ) -> None:
        self._runtime_sha_handler = runtime_sha_handler or HandlerRuntimeShaVerify()

    def handle(
        self, request: ModelIntegrationSweepOrchestratorRequest
    ) -> ModelIntegrationSweepOrchestratorResult:
        artifact_root = self._resolve_root(request.artifact_root)
        contracts_dir = self._resolve_dir(
            request.contracts_dir, artifact_root / "contracts"
        )
        receipts_dir = self._resolve_dir(
            request.receipts_dir, artifact_root / "drift" / "dod_receipts"
        )
        artifact_date = request.artifact_date or date.today().isoformat()
        artifact_path = (
            artifact_root / "drift" / "integration" / f"{artifact_date}.yaml"
        )
        tickets = [
            ticket.strip().upper() for ticket in request.tickets if ticket.strip()
        ]
        runtime_sha_records = (
            []
            if request.dry_run
            else self._run_runtime_sha_checks(
                tickets=tickets,
                contracts_dir=contracts_dir,
                receipts_dir=receipts_dir,
                request=request,
            )
        )
        stale_count = sum(
            1
            for record in runtime_sha_records
            if record.get("status") != EnumReceiptStatus.PASS.value
        )
        status = "blocked" if stale_count else "recorded"

        payload = {
            "artifact_type": "ModelIntegrationRecord",
            "artifact_version": "1.0.0",
            "date": artifact_date,
            "scope": request.scope or "explicit",
            "status": status,
            "tickets": tickets,
            "runtime_sha_match": runtime_sha_records,
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
            status=status,
            artifact_path=str(artifact_path),
            artifact_written=artifact_written,
            ticket_count=len(tickets),
            details={
                "dry_run": str(request.dry_run).lower(),
                "artifact_date": artifact_date,
                "runtime_sha_checks": str(len(runtime_sha_records)),
                "runtime_sha_stale": str(stale_count),
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

    @staticmethod
    def _resolve_dir(configured: str, default_path: Path) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        return default_path.resolve()

    def _run_runtime_sha_checks(
        self,
        *,
        tickets: list[str],
        contracts_dir: Path,
        receipts_dir: Path,
        request: ModelIntegrationSweepOrchestratorRequest,
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for ticket_id in tickets:
            contract_path = contracts_dir / f"{ticket_id}.yaml"
            if not contract_path.exists():
                continue
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                continue

            for evidence_item_id, merge_sha in self._iter_runtime_sha_checks(raw):
                receipt = self._runtime_sha_handler.handle(
                    ModelRuntimeShaVerifyRequest(
                        ticket_id=ticket_id,
                        evidence_item_id=evidence_item_id,
                        merge_sha=merge_sha,
                        runtime_host=request.runtime_host,
                        runtime_repo_path=request.runtime_repo_path,
                    )
                )
                receipt_path = (
                    receipts_dir
                    / ticket_id
                    / evidence_item_id
                    / f"{CHECK_TYPE_RUNTIME_SHA_MATCH}.yaml"
                )
                receipt_path.parent.mkdir(parents=True, exist_ok=True)
                receipt_path.write_text(
                    yaml.safe_dump(receipt.model_dump(mode="json"), sort_keys=True),
                    encoding="utf-8",
                )
                records.append(
                    {
                        "ticket_id": ticket_id,
                        "evidence_item_id": evidence_item_id,
                        "status": receipt.status.value,
                        "merge_sha": merge_sha,
                        "receipt_path": str(receipt_path),
                    }
                )
        return records

    @staticmethod
    def _iter_runtime_sha_checks(
        contract: dict[object, object],
    ) -> list[tuple[str, str]]:
        checks_to_run: list[tuple[str, str]] = []
        dod_evidence = contract.get("dod_evidence", [])
        if not isinstance(dod_evidence, list):
            return checks_to_run
        for item in dod_evidence:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            checks = item.get("checks", [])
            if not isinstance(item_id, str) or not isinstance(checks, list):
                continue
            for check in checks:
                if not isinstance(check, dict):
                    continue
                if check.get("check_type") != CHECK_TYPE_RUNTIME_SHA_MATCH:
                    continue
                check_value = check.get("check_value")
                if isinstance(check_value, str) and check_value.strip():
                    checks_to_run.append((item_id, check_value.strip()))
        return checks_to_run
