# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule ``cursor``: every projection_api exposure declares a cursor_column.

Ported from ``scripts/validation/check_projection_cursor_declared.py``
(OMN-18043). Two classes, exactly as the script decided them:

* a ratchet: an exposure with no ``cursor_column`` that is not in the frozen
  baseline FAILS; the baseline is typed input and only shrinks;
* a HARD class: a declared ``cursor_column`` that is absent from the exposure's
  own ``columns`` list FAILS unconditionally and is never baselined.

A baseline entry whose exposure now declares a cursor is reported as WARN so the
baseline can shrink; it never fails the run.
"""

from collections.abc import Sequence

import yaml
from omnibase_core.models.validation.model_validation_finding import (
    ModelValidationFinding,
)

from omnimarket.nodes.node_contract_projection_check_compute.handlers.findings import (
    RULE_CURSOR_MEMBERSHIP,
    RULE_CURSOR_MISSING,
    RULE_CURSOR_SHRINKABLE,
    RULE_UNPARSEABLE,
    make_finding,
)
from omnimarket.nodes.node_contract_projection_check_compute.models import (
    ModelProjectionNodeSources,
)


def _exposures(contract: dict[str, object]) -> list[dict[str, object]]:
    """Every exposed ``projection_api`` block, in both declared shapes (flat and nested)."""
    section = contract.get("projection_api")
    if not isinstance(section, dict) or section.get("expose") is not True:
        return []
    nested = section.get("exposures")
    if isinstance(nested, list):
        return [e for e in nested if isinstance(e, dict)]
    return [section]


def _membership_problem(exposure: dict[str, object]) -> str | None:
    cursor = exposure.get("cursor_column")
    if not cursor:
        return None
    columns = exposure.get("columns")
    if not isinstance(columns, list):
        return (
            f"cursor_column {cursor!r} is declared but the exposure declares no "
            "columns list"
        )
    if columns == ["*"]:
        return None
    declared = {c.strip('"') for c in columns if isinstance(c, str)}
    if not isinstance(cursor, str) or cursor.strip('"') not in declared:
        return (
            f"cursor_column {cursor!r} is not among the declared columns "
            f"{columns!r} (missing column: {cursor!r})"
        )
    return None


def tracked_exposures(
    nodes: Sequence[ModelProjectionNodeSources],
) -> tuple[list[tuple[str, dict[str, object]]], list[ModelValidationFinding]]:
    """``(exposure_id, exposure)`` in stable order, plus a WARN per unparseable contract."""
    tracked: list[tuple[str, dict[str, object]]] = []
    warnings: list[ModelValidationFinding] = []
    for node in sorted(nodes, key=lambda n: n.contract_path.split("/")):
        try:
            contract = yaml.safe_load(node.contract_text)
        except yaml.YAMLError as exc:
            warnings.append(
                make_finding(
                    rule_id=RULE_UNPARSEABLE,
                    message=f"skipping unparseable {node.contract_path}: {exc}",
                    location=node.contract_path,
                    severity="WARN",
                )
            )
            continue
        if not isinstance(contract, dict):
            continue
        for index, exposure in enumerate(_exposures(contract)):
            table = exposure.get("table") or "unnamed"
            tracked.append((f"{node.node}::{table}#{index}", exposure))
    return tracked, warnings


def missing_cursor_ids(tracked: list[tuple[str, dict[str, object]]]) -> list[str]:
    return sorted(i for i, e in tracked if not e.get("cursor_column"))


def membership_violations(
    tracked: list[tuple[str, dict[str, object]]],
) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for exposure_id, exposure in tracked:
        problem = _membership_problem(exposure)
        if problem is not None:
            found.append((exposure_id, problem))
    return sorted(found)


def check_cursor(
    nodes: Sequence[ModelProjectionNodeSources], baseline: Sequence[str]
) -> list[ModelValidationFinding]:
    tracked, findings = tracked_exposures(nodes)
    current = missing_cursor_ids(tracked)
    base = set(baseline)
    for exposure_id, reason in membership_violations(tracked):
        findings.append(
            make_finding(
                rule_id=RULE_CURSOR_MEMBERSHIP,
                message=f"{exposure_id}: {reason}",
                location=exposure_id,
            )
        )
    for exposure_id in sorted(set(current) - base):
        findings.append(
            make_finding(
                rule_id=RULE_CURSOR_MISSING, message=exposure_id, location=exposure_id
            )
        )
    for exposure_id in sorted(base - set(current)):
        findings.append(
            make_finding(
                rule_id=RULE_CURSOR_SHRINKABLE,
                message=exposure_id,
                location=exposure_id,
                severity="WARN",
            )
        )
    return findings
