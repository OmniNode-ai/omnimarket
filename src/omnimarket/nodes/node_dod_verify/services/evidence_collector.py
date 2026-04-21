# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Evidence collector — loads contract YAML, runs dod_evidence checks, returns results.

Responsibilities:
  1. Locate and load a ticket's contract YAML (auto-detect or explicit path).
  2. Iterate over ``dod_evidence[]`` items.
  3. For each item's ``checks[]``, execute the check.
  4. Return a list of ModelEvidenceCheckResult for the handler to tally.

This module is the I/O boundary — it reads files and runs subprocesses.
The handler itself remains pure (no I/O) and continues to work when callers
pre-populate evidence_results (tests, event-bus consumers).
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)

logger = logging.getLogger(__name__)

# Default contract search roots (first match wins)
_DEFAULT_CONTRACT_ROOTS: list[str] = [
    "${ONEX_CC_REPO_PATH}/contracts",
    "${OMNI_HOME}/onex_change_control/contracts",
]


class EvidenceCollector:
    """Loads a ticket contract and runs dod_evidence checks.

    Usage::

        collector = EvidenceCollector()
        results = collector.collect("OMN-9414")
        # results: list[ModelEvidenceCheckResult]
    """

    def __init__(self, timeout_per_check: int = 30) -> None:
        self._timeout = timeout_per_check

    def collect(
        self,
        ticket_id: str,
        contract_path: str | None = None,
    ) -> list[ModelEvidenceCheckResult]:
        """Load contract and run all dod_evidence checks.

        Args:
            ticket_id: Linear ticket ID (e.g. OMN-1234).
            contract_path: Explicit path to contract YAML. If None, auto-detect.

        Returns:
            One ModelEvidenceCheckResult per dod_evidence item.
        """
        if contract_path is not None:
            path = Path(contract_path)
            if not path.exists():
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"Contract file not found: {contract_path}",
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=f"File does not exist: {contract_path}",
                    )
                ]
        else:
            found = self._find_contract(ticket_id)
            if found is None:
                return [
                    ModelEvidenceCheckResult(
                        evidence_id="contract",
                        description=f"No contract found for {ticket_id}",
                        status=EnumEvidenceCheckStatus.SKIPPED,
                        message=(
                            f"Searched: {_DEFAULT_CONTRACT_ROOTS}. "
                            "Provide --contract-path or generate a contract."
                        ),
                    )
                ]
            path = found

        raw = self._load_yaml(path)
        if raw is None:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Failed to parse contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=f"YAML parse error in {path}",
                )
            ]

        # Validate contract belongs to the requested ticket
        contract_ticket_id = raw.get("ticket_id")
        if contract_ticket_id != ticket_id:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Contract ticket mismatch: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message=(
                        f"Expected ticket_id {ticket_id!r}, "
                        f"found {contract_ticket_id!r}."
                    ),
                )
            ]

        dod_items = raw.get("dod_evidence", [])
        if not isinstance(dod_items, list):
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"Invalid dod_evidence structure in contract: {path}",
                    status=EnumEvidenceCheckStatus.FAILED,
                    message="dod_evidence must be a list of mappings.",
                )
            ]
        if not dod_items:
            return [
                ModelEvidenceCheckResult(
                    evidence_id="contract",
                    description=f"No dod_evidence entries in contract: {path}",
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message="Contract has empty or missing dod_evidence[] section.",
                )
            ]

        results: list[ModelEvidenceCheckResult] = []
        for item in dod_items:
            result = self._check_evidence_item(item, ticket_id)
            results.append(result)

        return results

    def _find_contract(self, ticket_id: str) -> Path | None:
        """Search standard locations for a ticket contract."""
        for root_template in _DEFAULT_CONTRACT_ROOTS:
            root = Path(os.path.expandvars(root_template))
            candidate = root / f"{ticket_id}.yaml"
            if candidate.exists():
                logger.info("Found contract at %s", candidate)
                return candidate

        # Fallback: resolve via OMNI_HOME env var
        omni_home = os.environ.get("OMNI_HOME", str(Path.home() / "Code" / "omni_home"))
        candidate = (
            Path(omni_home) / "onex_change_control" / "contracts" / f"{ticket_id}.yaml"
        )
        if candidate.exists():
            logger.info("Found contract at %s", candidate)
            return candidate

        return None

    def _load_yaml(self, path: Path) -> dict[str, Any] | None:
        """Load and return YAML content, or None on error."""
        try:
            content = path.read_text(encoding="utf-8")
            raw = yaml.safe_load(content)
            if not isinstance(raw, dict):
                logger.error("Contract %s root is not a mapping", path)
                return None
            return raw
        except Exception as exc:
            logger.error("Failed to parse %s: %s", path, exc)
            return None

    def _check_evidence_item(
        self,
        item: dict[str, Any],
        ticket_id: str,
    ) -> ModelEvidenceCheckResult:
        """Run checks for a single dod_evidence item."""
        evidence_id = item.get("id", "unknown")
        description = item.get("description", evidence_id)
        checks = item.get("checks", [])

        if not isinstance(checks, list):
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.FAILED,
                message="checks must be a list of mappings.",
            )

        if not checks:
            return ModelEvidenceCheckResult(
                evidence_id=evidence_id,
                description=description,
                status=EnumEvidenceCheckStatus.SKIPPED,
                message="No checks defined for this evidence item.",
            )

        # Run each check; all must pass for the item to be VERIFIED
        messages: list[str] = []
        for check in checks:
            check_type = check.get("check_type", "unknown")
            if check_type == "command":
                ok, msg = self._run_command_check(check, ticket_id)
                if not ok:
                    return ModelEvidenceCheckResult(
                        evidence_id=evidence_id,
                        description=description,
                        status=EnumEvidenceCheckStatus.FAILED,
                        message=msg,
                    )
                messages.append(msg)
            else:
                messages.append(f"Unsupported check_type: {check_type}")
                return ModelEvidenceCheckResult(
                    evidence_id=evidence_id,
                    description=description,
                    status=EnumEvidenceCheckStatus.SKIPPED,
                    message=f"Unsupported check_type: {check_type}",
                )

        return ModelEvidenceCheckResult(
            evidence_id=evidence_id,
            description=description,
            status=EnumEvidenceCheckStatus.VERIFIED,
            message="; ".join(messages) if messages else None,
        )

    def _run_command_check(
        self,
        check: dict[str, Any],
        ticket_id: str,
    ) -> tuple[bool, str]:
        """Execute a command-type check. Returns (success, message)."""
        # Prefer explicit `command` field; fall back to `check_value`
        cmd_str = check.get("command") or check.get("check_value", "")
        if not cmd_str:
            return False, "Empty command in check definition."

        # Template substitution for common placeholders (escaped to prevent shell injection)
        cmd_str = cmd_str.replace("{ticket_id}", shlex.quote(ticket_id))

        logger.info("Running command check: %s", cmd_str)

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            elapsed_ms = int((time.monotonic() - start) * 1000)
        except subprocess.TimeoutExpired:
            return False, f"Timed out after {self._timeout}s: {cmd_str}"
        except Exception as exc:
            return False, f"Execution error: {exc}"

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            return False, f"FAILED ({elapsed_ms}ms): {detail}"

        return True, f"OK ({elapsed_ms}ms): {stdout[:200]}"


__all__ = ["EvidenceCollector"]
