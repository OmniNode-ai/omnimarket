import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.ticket.enum_receipt_status import EnumReceiptStatus
from omnibase_core.validation.runtime_sha_match import CHECK_TYPE_RUNTIME_SHA_MATCH

from omnimarket.nodes.node_dod_verify.handlers.handler_runtime_sha_verify import (
    HandlerRuntimeShaVerify,
    ModelRuntimeShaVerifyRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.handlers.surface_probes import (
    probe_container_health,
    probe_db_tables,
    probe_github_ci,
    probe_golden_chain,
    probe_kafka_topics,
    probe_projection_api,
    probe_runtime_health,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_request import (
    ModelIntegrationSweepOrchestratorRequest,
)
from omnimarket.nodes.node_integration_sweep_orchestrator.models.model_integration_sweep_orchestrator_result import (
    ModelIntegrationSweepOrchestratorResult,
)

# Terminal sweep statuses (OMN-13924). ``no_input`` is the typed non-success
# state for a sweep that resolved ZERO probe targets and ZERO runtime-SHA
# checks — a run that verified nothing must never report ``recorded``.
STATUS_RECORDED = "recorded"
STATUS_BLOCKED = "blocked"
STATUS_PLANNED = "planned"
STATUS_NO_INPUT = "no_input"

# Per-entry status for dry-run plan records.
PROBE_STATUS_PLANNED = "planned"
PROBE_STATUS_INVALID = "invalid"

# Per-entry status values a live surface probe (surface_probes.py) reports
# for a dimension it actually scanned but did NOT verify (OMN-14538). Both
# are terminal non-success outcomes for that surface — a probe never raises,
# it always returns a structured "fail" (assertion did not hold) or "error"
# (could not reach the surface at all) dict instead.
_SURFACE_NON_SUCCESS_STATUSES = frozenset({"fail", "error"})


class HandlerIntegrationSweepOrchestrator:
    """Write deterministic integration sweep artifacts."""

    _SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

    @classmethod
    def _safe_segment(cls, value: str, field: str) -> str:
        if not cls._SAFE_SEGMENT_RE.fullmatch(value):
            raise ValueError(f"Invalid {field}: {value!r}")
        return value

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
            self._plan_runtime_sha_checks(tickets=tickets, contracts_dir=contracts_dir)
            if request.dry_run
            else self._run_runtime_sha_checks(
                tickets=tickets,
                contracts_dir=contracts_dir,
                receipts_dir=receipts_dir,
                request=request,
            )
        )
        stale_count = (
            0
            if request.dry_run
            else sum(
                1
                for record in runtime_sha_records
                if record.get("status") != EnumReceiptStatus.PASS.value
            )
        )

        surface_results: list[dict[str, Any]]
        if not request.run_surface_probes:
            surface_results = []
        elif request.dry_run:
            surface_results = self._plan_surface_probes(request)
        else:
            surface_results = self._run_surface_probes(request)

        invalid_target_count = sum(
            1
            for entry in surface_results
            if entry.get("status") == PROBE_STATUS_INVALID
        )
        # OMN-14538: a probe that RAN and reported "fail"/"error" is scanned
        # evidence, not silence — it must never be absorbed into a green
        # verdict. surface_probes.py never raises, so a dead SSH host, an
        # unreachable runtime, or a failing rpk/psql assertion all surface
        # here as a structured non-success entry rather than an exception.
        surface_fail_count = sum(
            1
            for entry in surface_results
            if entry.get("status") in _SURFACE_NON_SUCCESS_STATUSES
        )
        # OMN-14538: the census of WHAT was actually placed in scope for this
        # run. tickets and the four infra-surface census lists (kafka/db/
        # projection/golden_chain) are each an optional-with-empty-default
        # field — a caller can construct a request that declares nothing on
        # every one of them and still get 3 baseline health/CI probes to
        # execute. Those 3 probes prove infra reachability, never integration
        # correctness. A RECORDED verdict asserts the latter, so it requires
        # at least one positively-declared census dimension.
        census_dimensions_configured = sum(
            1
            for configured in (
                tickets,
                request.kafka_topics or request.kafka_consumer_groups,
                request.db_tables,
                request.projection_topics,
                request.golden_chains,
            )
            if configured
        )
        scanned_count = len(surface_results) + len(runtime_sha_records)
        status = self._resolve_status(
            dry_run=request.dry_run,
            probe_count=scanned_count,
            stale_count=stale_count,
            invalid_target_count=invalid_target_count,
            surface_fail_count=surface_fail_count,
            census_dimensions_configured=census_dimensions_configured,
        )

        payload = {
            "artifact_type": "ModelIntegrationRecord",
            "artifact_version": "1.0.0",
            "date": artifact_date,
            "scope": request.scope or "explicit",
            "status": status,
            "tickets": tickets,
            "runtime_sha_match": runtime_sha_records,
            "surfaces": surface_results,
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
            surfaces=surface_results,
            details={
                "dry_run": str(request.dry_run).lower(),
                "artifact_date": artifact_date,
                "runtime_sha_checks": str(len(runtime_sha_records)),
                "runtime_sha_stale": str(stale_count),
                "surface_probe_count": str(len(surface_results)),
                "surface_probe_failures": str(surface_fail_count),
                "invalid_probe_targets": str(invalid_target_count),
                "scanned_count": str(scanned_count),
                "census_dimensions_configured": str(census_dimensions_configured),
            },
        )

    @staticmethod
    def _resolve_status(
        *,
        dry_run: bool,
        probe_count: int,
        stale_count: int,
        invalid_target_count: int,
        surface_fail_count: int,
        census_dimensions_configured: int,
    ) -> str:
        """Resolve the terminal sweep status.

        Zero resolved probe targets AND zero runtime-SHA checks is NOT a
        success: the sweep verified nothing, so it terminates ``no_input``
        (OMN-13924). Dry-run reports ``planned`` (or ``blocked`` when a
        planned probe has an empty/invalid target); wet runs keep the
        ``blocked``/``recorded`` semantics.

        OMN-14538 (class fix): a wet run can only terminate ``recorded`` when
        BOTH of the following hold:

        1. Every dimension that was actually scanned reported success —
           a stale runtime-SHA receipt OR a surface probe that returned
           ``fail``/``error`` (RUNTIME fail, SSH-dead CONTAINER_HEALTH,
           unreachable KAFKA/DB, etc.) fail-closes to ``blocked``. Previously
           only ``stale_count`` gated this, so RUNTIME fail + SSH dead +
           KAFKA fail + empty tables still returned ``recorded`` as long as
           no ticket's runtime-SHA receipt happened to be stale.
        2. At least one census dimension (tickets, kafka, db, projection,
           golden_chains) was positively declared. A run with an empty
           census on every dimension only ever executes the 3 baseline
           health/CI probes — real evidence of infra reachability, but zero
           evidence of integration correctness — so it can never claim
           ``recorded``. Absence of the census is a failure, not a silent
           pass-through.
        """
        if probe_count == 0:
            return STATUS_NO_INPUT
        if dry_run:
            return STATUS_BLOCKED if invalid_target_count else STATUS_PLANNED
        if not census_dimensions_configured:
            return STATUS_BLOCKED
        return (
            STATUS_BLOCKED if (stale_count or surface_fail_count) else STATUS_RECORDED
        )

    @staticmethod
    def _plan_surface_probes(
        request: ModelIntegrationSweepOrchestratorRequest,
    ) -> list[dict[str, Any]]:
        """Enumerate and validate the probes a wet run would execute.

        Mirrors the gating logic of ``_run_surface_probes`` exactly — the
        baseline RUNTIME_HEALTH / CONTAINER_HEALTH / GITHUB_CI probes are
        always planned, and KAFKA / DB / PROJECTION / GOLDEN_CHAIN are planned
        when their config lists are populated — but touches no surface. Each
        entry carries the resolved probe target so a dry-run receipt shows the
        real plan instead of an empty ``surfaces`` list (OMN-13924).
        """

        def plan(surface: str, target: str, **extra: Any) -> dict[str, Any]:
            entry: dict[str, Any] = {
                "surface": surface,
                "status": (
                    PROBE_STATUS_PLANNED if target.strip() else PROBE_STATUS_INVALID
                ),
                "target": target,
            }
            if not target.strip():
                entry["reason"] = "empty probe target"
            entry.update(extra)
            return entry

        plans = [
            plan("RUNTIME_HEALTH", request.stability_test_runtime_url),
            plan("CONTAINER_HEALTH", request.container_health_host),
            plan("GITHUB_CI", request.github_ci_repo),
        ]
        if request.kafka_topics or request.kafka_consumer_groups:
            plans.append(
                plan(
                    "KAFKA",
                    request.infra_runtime_host,
                    container=request.redpanda_container,
                    topics=list(request.kafka_topics),
                    consumer_groups=list(request.kafka_consumer_groups),
                )
            )
        if request.db_tables:
            plans.append(
                plan(
                    "DB",
                    request.infra_runtime_host,
                    container=request.postgres_container,
                    database=request.db_database,
                    tables=list(request.db_tables),
                )
            )
        if request.projection_topics:
            plans.append(
                plan(
                    "PROJECTION",
                    request.projection_api_url,
                    topics=list(request.projection_topics),
                )
            )
        for chain in request.golden_chains:
            plans.append(
                plan(
                    "GOLDEN_CHAIN",
                    request.infra_runtime_host,
                    chain_name=chain.chain_name,
                    command_topic=chain.command_topic,
                    consumer_group=chain.consumer_group,
                    tail_table=chain.tail_table,
                )
            )
        return plans

    def _plan_runtime_sha_checks(
        self,
        *,
        tickets: list[str],
        contracts_dir: Path,
    ) -> list[dict[str, str]]:
        """Enumerate the runtime-SHA checks a wet run would execute (no SSH)."""
        return [
            {
                "ticket_id": ticket_id,
                "evidence_item_id": evidence_item_id,
                "status": PROBE_STATUS_PLANNED,
                "merge_sha": merge_sha,
            }
            for ticket_id, evidence_item_id, merge_sha in self._enumerate_ticket_sha_checks(
                tickets=tickets, contracts_dir=contracts_dir
            )
        ]

    @staticmethod
    def _run_surface_probes(
        request: ModelIntegrationSweepOrchestratorRequest,
    ) -> list[dict[str, Any]]:
        """Execute the configured surface probes.

        RUNTIME_HEALTH, CONTAINER_HEALTH, and GITHUB_CI always run (the health/CI
        baseline). The KAFKA, DB, PROJECTION, and GOLDEN_CHAIN probes run only
        when their config lists are populated, so an unconfigured caller still
        gets the baseline and never a spurious infra-probe failure.
        """
        results: list[dict[str, Any]] = []
        results.append(probe_runtime_health(request.stability_test_runtime_url))
        results.append(probe_container_health(request.container_health_host))
        results.append(probe_github_ci(request.github_ci_repo))

        if request.kafka_topics or request.kafka_consumer_groups:
            results.append(
                probe_kafka_topics(
                    request.infra_runtime_host,
                    request.redpanda_container,
                    request.kafka_topics,
                    request.kafka_consumer_groups,
                )
            )
        if request.db_tables:
            results.append(
                probe_db_tables(
                    request.infra_runtime_host,
                    request.postgres_container,
                    request.postgres_user,
                    request.db_database,
                    request.db_tables,
                )
            )
        if request.projection_topics:
            results.append(
                probe_projection_api(
                    request.projection_api_url,
                    request.projection_topics,
                )
            )
        for chain in request.golden_chains:
            results.append(
                probe_golden_chain(
                    runtime_host=request.infra_runtime_host,
                    redpanda_container=request.redpanda_container,
                    postgres_container=request.postgres_container,
                    postgres_user=request.postgres_user,
                    chain_name=chain.chain_name,
                    command_topic=chain.command_topic,
                    consumer_group=chain.consumer_group,
                    tail_database=chain.tail_database,
                    tail_table=chain.tail_table,
                )
            )
        return results

    @staticmethod
    def _resolve_root(configured: str) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        env_root = os.environ.get("ONEX_CC_REPO_PATH")  # contract-config-ok: config  # fmt: skip
        if env_root:
            resolved = Path(env_root).expanduser().resolve()
            if resolved.exists() and (resolved / "contracts").is_dir():
                return resolved
            # A stale/container ONEX_CC_REPO_PATH (e.g. the in-container mount
            # /onex_change_control) can leak into a local infra-venv run where
            # it has no contracts/ dir. Fall back to the canonical registry
            # clone under OMNI_HOME before failing so the sweep stays runnable
            # locally. Fail-fast (CLAUDE.md rule #8) is preserved:
            # os.environ["OMNI_HOME"] raises KeyError when unset — never a
            # silent default — and a fallback that itself lacks contracts/
            # still raises RuntimeError.
            fallback = (
                Path(os.environ["OMNI_HOME"]).expanduser().resolve()
                / "onex_change_control"
            )
            if fallback.exists() and (fallback / "contracts").is_dir():
                return fallback
            raise RuntimeError(
                f"ONEX_CC_REPO_PATH={env_root!r} resolves to {resolved} which "
                "does not exist or lacks a contracts/ directory, and the "
                f"OMNI_HOME fallback {fallback} also lacks a contracts/ directory"
            )
        raise RuntimeError(
            "ONEX_CC_REPO_PATH is not set and no explicit artifact_root was provided. "
            "Set ONEX_CC_REPO_PATH to the omni_home repo registry path."
        )

    @staticmethod
    def _resolve_dir(configured: str, default_path: Path) -> Path:
        if configured:
            return Path(configured).expanduser().resolve()
        return default_path.resolve()

    def _enumerate_ticket_sha_checks(
        self,
        *,
        tickets: list[str],
        contracts_dir: Path,
    ) -> list[tuple[str, str, str]]:
        """Resolve ``(ticket_id, evidence_item_id, merge_sha)`` triples.

        Shared by the dry-run planner and the wet executor so both enumerate
        (and safe-segment-validate) exactly the same check set.
        """
        triples: list[tuple[str, str, str]] = []
        for ticket_id in tickets:
            safe_ticket_id = self._safe_segment(ticket_id, "ticket_id")
            contract_path = contracts_dir / f"{safe_ticket_id}.yaml"
            if not contract_path.exists():
                continue
            raw = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                continue
            for evidence_item_id, merge_sha in self._iter_runtime_sha_checks(raw):
                self._safe_segment(evidence_item_id, "evidence_item_id")
                triples.append((ticket_id, evidence_item_id, merge_sha))
        return triples

    def _run_runtime_sha_checks(
        self,
        *,
        tickets: list[str],
        contracts_dir: Path,
        receipts_dir: Path,
        request: ModelIntegrationSweepOrchestratorRequest,
    ) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for ticket_id, evidence_item_id, merge_sha in self._enumerate_ticket_sha_checks(
            tickets=tickets, contracts_dir=contracts_dir
        ):
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
