from __future__ import annotations

import json
import re
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import yaml

from omnimarket.nodes.node_gap_compute.models.model_gap_compute_request import (
    EnumGapSubcommand,
    ModelGapComputeRequest,
)
from omnimarket.nodes.node_gap_compute.models.model_gap_compute_result import (
    EnumGapCategory,
    EnumGapConfidence,
    EnumGapSeverity,
    EnumGapStatus,
    ModelGapComputeResult,
    ModelGapFinding,
    ModelSkippedGapProbe,
)

_REPO_ROOT = Path(__file__).resolve().parents[5]
_OMNI_HOME = _REPO_ROOT.parent
_TOPIC_RE = re.compile(
    r"^onex\.(cmd|evt|intent)\.[a-z0-9_-]+(?:\.[a-z0-9][a-z0-9_-]*)+\.v[0-9]+$"
)


class HandlerGapCompute:
    """Run deterministic gap probes over local repo contracts."""

    def handle(self, request: ModelGapComputeRequest) -> ModelGapComputeResult:
        if request.subcommand == EnumGapSubcommand.DETECT:
            return self._detect(request)
        if request.subcommand in {EnumGapSubcommand.FIX, EnumGapSubcommand.RECONCILE}:
            return self._classify_report(request)
        if request.subcommand == EnumGapSubcommand.CYCLE:
            detected = self._detect(request)
            if (
                request.no_fix
                or request.dry_run
                or detected.status == EnumGapStatus.CLEAN
            ):
                return detected
            return self._classify_report(request, detected=detected)
        msg = f"Unsupported gap subcommand: {request.subcommand}"
        raise ValueError(msg)

    def _detect(self, request: ModelGapComputeRequest) -> ModelGapComputeResult:
        repo_roots = self._resolve_repo_roots(request)
        if not repo_roots:
            return ModelGapComputeResult(
                status=EnumGapStatus.BLOCKED,
                run_id=self._run_id(request, []),
                message="No repo roots were available for deterministic gap detection.",
                subcommand=request.subcommand.value,
                scope=request.scope,
                dry_run=request.dry_run,
                skipped_probes=[
                    ModelSkippedGapProbe(
                        probe="intake",
                        reason="NO_REPO_EVIDENCE",
                    )
                ],
            )

        findings: list[ModelGapFinding] = []
        best_effort: list[ModelGapFinding] = []
        skipped_probes = self._skipped_live_probes(request)
        contracts_checked = 0
        for repo_root in repo_roots:
            repo_name = repo_root.name
            for contract_path in sorted(repo_root.rglob("contract.yaml")):
                if (
                    ".venv" in contract_path.parts
                    or "node_modules" in contract_path.parts
                ):
                    continue
                rel_path = _relative_to_repo(contract_path, repo_root)
                try:
                    raw = (
                        yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
                    )
                except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                    skipped_probes.append(
                        ModelSkippedGapProbe(
                            probe="contract_parse",
                            reason=f"{rel_path}: {type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                if not isinstance(raw, dict):
                    continue
                contracts_checked += 1
                node_name = str(raw.get("name") or contract_path.parent.name)
                if raw.get("node_not_implemented") is True:
                    findings.append(
                        self._finding(
                            category=EnumGapCategory.MISSING_NODE_TYPE,
                            boundary_kind="node_implementation",
                            rule_name="node_not_implemented",
                            severity=EnumGapSeverity.WARNING,
                            repo=repo_name,
                            path=rel_path,
                            message=f"{node_name} is still marked node_not_implemented.",
                            proof={"node": node_name},
                        )
                    )
                event_bus = raw.get("event_bus")
                if not isinstance(event_bus, dict):
                    findings.append(
                        self._finding(
                            category=EnumGapCategory.CONTRACT_DRIFT,
                            boundary_kind="kafka_topic",
                            rule_name="missing_event_bus_contract",
                            severity=EnumGapSeverity.WARNING,
                            repo=repo_name,
                            path=rel_path,
                            message=f"{node_name} has no event_bus declaration.",
                            proof={"node": node_name},
                        )
                    )
                    continue
                for topic in _contract_topics(event_bus, raw):
                    if not _TOPIC_RE.match(topic):
                        findings.append(
                            self._finding(
                                category=EnumGapCategory.CONTRACT_DRIFT,
                                boundary_kind="kafka_topic",
                                rule_name="topic_name_mismatch",
                                severity=EnumGapSeverity.CRITICAL,
                                repo=repo_name,
                                path=rel_path,
                                message=f"{node_name} declares non-canonical topic {topic}.",
                                proof={"node": node_name, "topic": topic},
                            )
                        )

        best_effort = best_effort[: request.max_best_effort]
        findings = self._filter_findings(findings, request)[: request.max_findings]
        status = (
            EnumGapStatus.CLEAN
            if not findings and not best_effort
            else EnumGapStatus.FINDINGS
        )
        return ModelGapComputeResult(
            status=status,
            run_id=self._run_id(request, repo_roots),
            message=(
                f"Scanned {contracts_checked} contracts across {len(repo_roots)} repos; "
                f"{len(findings) + len(best_effort)} findings."
            ),
            subcommand=request.subcommand.value,
            scope=request.scope,
            dry_run=request.dry_run,
            repos_in_scope=[root.name for root in repo_roots],
            contracts_checked=contracts_checked,
            findings=findings,
            best_effort_findings=best_effort,
            skipped_probes=skipped_probes,
        )

    def _classify_report(
        self,
        request: ModelGapComputeRequest,
        *,
        detected: ModelGapComputeResult | None = None,
    ) -> ModelGapComputeResult:
        report_path = Path(request.report or request.resume or "")
        if detected is None and (not str(report_path) or not report_path.is_file()):
            return ModelGapComputeResult(
                status=EnumGapStatus.BLOCKED,
                run_id=self._run_id(request, []),
                message="Gap fix/reconcile requires an existing report artifact.",
                subcommand=request.subcommand.value,
                scope=request.scope,
                dry_run=request.dry_run,
                report_path=str(report_path) if str(report_path) else None,
                skipped_probes=[
                    ModelSkippedGapProbe(probe="fix", reason="REPORT_REQUIRED")
                ],
            )

        payload: dict[str, object] = {}
        if report_path.is_file():
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        source_findings = (
            detected.findings + detected.best_effort_findings
            if detected is not None
            else []
        )
        if not source_findings:
            raw_findings = payload.get("findings", [])
            if isinstance(raw_findings, list):
                source_findings = [
                    _finding_from_report(item)
                    for item in raw_findings
                    if isinstance(item, dict)
                ]

        counts = {"AUTO": 0, "GATE": 0}
        for finding in source_findings:
            dispatch_class = _dispatch_class(finding.boundary_kind, finding.rule_name)
            counts[dispatch_class] = counts.get(dispatch_class, 0) + 1

        status = EnumGapStatus.CLEAN if not source_findings else EnumGapStatus.FINDINGS
        return ModelGapComputeResult(
            status=status,
            run_id=self._run_id(request, []),
            message=(
                f"Classified {len(source_findings)} findings for "
                f"{request.subcommand.value}; AUTO={counts.get('AUTO', 0)} "
                f"GATE={counts.get('GATE', 0)}."
            ),
            subcommand=request.subcommand.value,
            scope=request.scope,
            dry_run=request.dry_run,
            findings=source_findings,
            report_path=str(report_path) if report_path.is_file() else None,
            dispatch_class_counts=counts,
        )

    def _resolve_repo_roots(self, request: ModelGapComputeRequest) -> list[Path]:
        if request.repo_roots:
            roots = [Path(item) for item in request.repo_roots]
        else:
            roots = [_REPO_ROOT]
        if request.repo:
            roots = [path for path in roots if path.name == request.repo]
        return sorted(path for path in roots if path.is_dir())

    def _filter_findings(
        self, findings: list[ModelGapFinding], request: ModelGapComputeRequest
    ) -> list[ModelGapFinding]:
        if request.severity_threshold == "CRITICAL":
            return [
                finding
                for finding in findings
                if finding.severity == EnumGapSeverity.CRITICAL
            ]
        return findings

    def _skipped_live_probes(
        self, request: ModelGapComputeRequest
    ) -> list[ModelSkippedGapProbe]:
        skipped = [
            ModelSkippedGapProbe(
                probe="projection_lag",
                reason="live infrastructure probe is outside deterministic compute path",
            ),
            ModelSkippedGapProbe(
                probe="migration_parity",
                reason="live infrastructure probe is outside deterministic compute path",
            ),
        ]
        if not request.include_auth_probes:
            skipped.append(
                ModelSkippedGapProbe(
                    probe="auth_config",
                    reason="auth_config requires explicit credential access",
                )
            )
        return (
            skipped
            if request.skip_infra_probes or not request.include_auth_probes
            else []
        )

    def _finding(
        self,
        *,
        category: EnumGapCategory,
        boundary_kind: str,
        rule_name: str,
        severity: EnumGapSeverity,
        repo: str,
        path: str,
        message: str,
        proof: dict[str, object],
    ) -> ModelGapFinding:
        finding_id = (
            "GAP-"
            + sha256(
                "|".join(
                    [category, boundary_kind, rule_name, repo, path, message]
                ).encode()
            ).hexdigest()[:8]
        )
        return ModelGapFinding(
            finding_id=finding_id,
            category=category,
            boundary_kind=boundary_kind,
            rule_name=rule_name,
            severity=severity,
            confidence=EnumGapConfidence.DETERMINISTIC,
            repo=repo,
            path=path,
            message=message,
            proof=proof,
        )

    def _run_id(self, request: ModelGapComputeRequest, roots: list[Path]) -> str:
        return str(
            uuid5(
                NAMESPACE_URL,
                "|".join(
                    [
                        "node_gap_compute",
                        request.subcommand.value,
                        request.scope,
                        *(str(path) for path in roots),
                    ]
                ),
            )
        )


def _contract_topics(event_bus: dict[str, object], raw: dict[str, object]) -> list[str]:
    topics: list[str] = []
    for key in ("publish_topics", "subscribe_topics"):
        value = event_bus.get(key)
        if isinstance(value, list):
            topics.extend(item for item in value if isinstance(item, str))
    terminal_event = raw.get("terminal_event")
    if isinstance(terminal_event, str):
        topics.append(terminal_event)
    return list(dict.fromkeys(topics))


def _relative_to_repo(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def _dispatch_class(boundary_kind: str, rule_name: str) -> str:
    auto_pairs = {
        ("kafka_topic", "topic_name_mismatch"),
        ("db_url_drift", "legacy_db_name_in_tests"),
        ("db_url_drift", "legacy_env_var"),
        ("legacy_config", "legacy_denylist_match"),
        ("branch_protection", "required_check_name_stale"),
    }
    return "AUTO" if (boundary_kind, rule_name) in auto_pairs else "GATE"


def _finding_from_report(item: dict[str, object]) -> ModelGapFinding:
    category = EnumGapCategory(str(item.get("category") or "CONTRACT_DRIFT"))
    boundary_kind = str(item.get("boundary_kind") or "unknown")
    rule_name = str(item.get("rule_name") or "unknown")
    repos_value = item.get("repos")
    repo = (
        str(repos_value[0])
        if isinstance(repos_value, list) and repos_value
        else "unknown"
    )
    message = str(item.get("message") or item.get("mismatch_shape") or rule_name)
    finding_id = str(item.get("finding_id") or item.get("fingerprint") or "")
    if not finding_id:
        finding_id = (
            "GAP-"
            + sha256(
                "|".join(
                    [str(category), boundary_kind, rule_name, repo, message]
                ).encode()
            ).hexdigest()[:8]
        )
    return ModelGapFinding(
        finding_id=finding_id,
        category=category,
        boundary_kind=boundary_kind,
        rule_name=rule_name,
        severity=EnumGapSeverity(str(item.get("severity") or "WARNING")),
        confidence=EnumGapConfidence(str(item.get("confidence") or "DETERMINISTIC")),
        repo=repo,
        path=str(item.get("repo_relative_path") or ""),
        message=message,
        proof=item.get("proof") if isinstance(item.get("proof"), dict) else {},
    )


__all__ = ["HandlerGapCompute"]
