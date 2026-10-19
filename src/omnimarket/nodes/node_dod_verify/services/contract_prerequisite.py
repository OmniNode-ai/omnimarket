# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check-only contract prerequisites shared by the verifier and PR authoring."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, cast

import yaml
from omnibase_core.utils.util_safe_yaml_loader import load_yaml_mapping_no_duplicates

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
    ModelEvidenceCheckResult,
)

M4_REPOSITORIES = frozenset(
    {"omnibase_core", "omnibase_infra", "omnimarket", "omniclaude", "omnidash"}
)

# Executables the verifier host (the hosted runner) provides for a command
# check. The PR writer has no docker, and a container smoke runs only where the
# check is verified, so authoring judges these names as commands (OMN-20910).
VERIFIER_HOST_TOOLS = frozenset({"docker"})


def cited_tickets(text: str) -> tuple[str, ...]:
    """The receipt-gate title rule: sorted, distinct, case-sensitive OMN ids."""
    return tuple(sorted(set(re.findall(r"OMN-[0-9]+", text))))


def missing_contract_message(ticket: str) -> str:
    """The receipt-gate refusal, also used before a push or a PR exists."""
    return f"pull request cites {ticket} but carries no contracts/{ticket}.yaml"


def contract_structure_failure(
    ticket_id: str, raw: dict[str, Any], path: Path | None
) -> ModelEvidenceCheckResult | None:
    """The collector's existing ticket/evidence checks, without executing checks."""
    contract_ticket_id = raw.get("ticket_id")
    if contract_ticket_id != ticket_id:
        return ModelEvidenceCheckResult(
            evidence_id="contract",
            description=f"Contract ticket mismatch: {path}",
            status=EnumEvidenceCheckStatus.FAILED,
            message=(
                f"Expected ticket_id {ticket_id!r}, found {contract_ticket_id!r}."
            ),
        )
    dod_items = raw.get("dod_evidence", [])
    if not isinstance(dod_items, list):
        return ModelEvidenceCheckResult(
            evidence_id="contract",
            description=f"Invalid dod_evidence structure in contract: {path}",
            status=EnumEvidenceCheckStatus.FAILED,
            message="dod_evidence must be a list of mappings.",
        )
    if not dod_items:
        return ModelEvidenceCheckResult(
            evidence_id="contract",
            description=f"No dod_evidence entries in contract: {path}",
            status=EnumEvidenceCheckStatus.SKIPPED,
            message="Contract has empty or missing dod_evidence[] section.",
        )
    return None


def contract_prerequisite(ticket: str, text: str | None) -> str | None:
    """Presence and static verifier prerequisites; never run declared commands."""
    from omnimarket.nodes.node_dod_verify.services.ac_falsifier_checks import (
        derive_falsifier_items,
    )
    from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
        _invalid_check_value_reason,
    )

    if text is None:
        return missing_contract_message(ticket)
    path = Path("contracts") / f"{ticket}.yaml"
    try:
        raw = cast(
            "dict[str, Any]", load_yaml_mapping_no_duplicates(text, source=str(path))
        )
    except (yaml.YAMLError, ValueError) as exc:
        return f"{path}: {exc}"
    failure = contract_structure_failure(ticket, raw, path)
    if failure is not None:
        return f"{path}: {failure.message}"
    items = raw["dod_evidence"]
    assert isinstance(items, list)  # contract_structure_failure checked this.
    for item in items:
        if not isinstance(item, dict):
            return f"{path}: dod_evidence must be a list of mappings."
        checks = item.get("checks")
        if not isinstance(checks, list) or not checks:
            return f"{path}: evidence checks must be a non-empty list."
        for check in checks:
            if not isinstance(check, dict):
                return f"{path}: evidence checks must be mappings."
            if check.get("check_type") in {"command", "test_passes"}:
                value = check.get("command") or check.get("check_value") or ""
                if not isinstance(value, str):
                    return f"{path}: check value must be a string."
                reason = _invalid_check_value_reason(
                    value, verifier_host_tools=VERIFIER_HOST_TOOLS
                )
                if reason:
                    return f"{path}: INVALID_CHECK_VALUE_NOT_A_COMMAND: {reason}"
    # Reuse the verifier's binding/retirement logic. Authorship must declare
    # evidence per AC; preflight does not mint or execute derived falsifiers.
    _, summary = derive_falsifier_items(
        raw,
        items,
        repo_candidates=(),
        path_exists=lambda _repo, _selector: False,
        declared_runner=lambda _repo, _selector: None,
    )
    if summary.unbound_criteria:
        return f"{path}: unbound acceptance criteria: {', '.join(summary.unbound_criteria)}"
    return None
