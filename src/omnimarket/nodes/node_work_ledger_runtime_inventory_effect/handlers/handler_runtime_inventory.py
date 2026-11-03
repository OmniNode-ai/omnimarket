# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for ``node_work_ledger_runtime_inventory_effect``.

Loads a runtime inventory overlay and a recorded host snapshot from where the request, the
environment or the overlay roots say, and checks them: the shape of every row and exclusion,
distinct non-blank DNS-style tenant slugs that reuse no reserved slug, a reason on every
exclusion, the required exclusion classes, and the reconciliation of the snapshot's observed
tokens against the rows and exclusions. The node ships the schema only; the data is the
overlay's.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from omnimarket.nodes.node_work_ledger_runtime_inventory_effect.models.model_runtime_inventory import (
    ModelRuntimeInventoryRequest,
    ModelRuntimeInventoryResult,
)

INVENTORY_SCHEMA = "work_ledger_runtime_inventory.v1"
SNAPSHOT_SCHEMA = "work_ledger_runtime_snapshot.v1"
OVERLAY_DIR_NAME = "work_ledger_runtime_inventory"
INVENTORY_FILE_NAME = "inventory.yaml"
INVENTORY_FILE_ENV = "ONEX_WORK_LEDGER_RUNTIME_INVENTORY_FILE"
OVERLAY_ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"

ROW_FIELDS = (
    "id",
    "host",
    "lane",
    "overlay",
    "current_tenant",
    "proposed_tenant_slug",
    "forwarder_instance",
)
EXCLUSION_FIELDS = ("id", "class", "reason")
SLUG_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _mapping_list(
    document: dict[str, Any], key: str, findings: list[str]
) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        findings.append(f"{key}: must be a list of mappings")
        return []
    return list(value)


def _matches(entry: dict[str, Any], label: str, findings: list[str]) -> list[str]:
    value = entry.get("matches")
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        findings.append(f"{label}: matches must be a list of non-blank strings")
        return []
    return list(value)


def _check_rows(document: dict[str, Any], findings: list[str]) -> dict[str, list[str]]:
    """Check the rows; return each row's matches keyed by row id."""
    claims: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    seen_slugs: set[str] = set()
    reserved = {str(slug) for slug in document.get("reserved_tenant_slugs") or []}
    for index, row in enumerate(_mapping_list(document, "rows", findings)):
        label = f"rows[{index}]"
        for field in ROW_FIELDS:
            if _blank(row.get(field)):
                findings.append(f"{label}: {field} is missing or blank")
        row_id = str(row.get("id", "")).strip()
        label = f"row {row_id or index}"
        if row_id in seen_ids:
            findings.append(f"{label}: duplicate row id")
        seen_ids.add(row_id)
        slug = row.get("proposed_tenant_slug")
        if isinstance(slug, str) and slug.strip():
            if not SLUG_PATTERN.fullmatch(slug):
                findings.append(f"{label}: slug {slug!r} is not a DNS-style label")
            if slug in seen_slugs:
                findings.append(f"{label}: duplicate tenant slug {slug!r}")
            if slug in reserved:
                findings.append(f"{label}: slug {slug!r} reuses a reserved tenant slug")
            seen_slugs.add(slug)
        claims[row_id or f"#{index}"] = _matches(row, label, findings)
    if not claims:
        findings.append("rows: the inventory has no rows")
    return claims


def _check_exclusions(
    document: dict[str, Any], findings: list[str]
) -> dict[str, list[str]]:
    claims: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    classes: set[str] = set()
    for index, entry in enumerate(_mapping_list(document, "exclusions", findings)):
        label = f"exclusions[{index}]"
        for field in EXCLUSION_FIELDS:
            if _blank(entry.get(field)):
                findings.append(f"{label}: {field} is missing or blank")
        entry_id = str(entry.get("id", "")).strip()
        label = f"exclusion {entry_id or index}"
        if entry_id in seen_ids:
            findings.append(f"{label}: duplicate exclusion id")
        seen_ids.add(entry_id)
        if isinstance(entry.get("class"), str):
            classes.add(entry["class"])
        matches = entry.get("matches")
        if matches == []:
            claims[entry_id or f"#{index}"] = []
        else:
            claims[entry_id or f"#{index}"] = _matches(entry, label, findings)
    for required in document.get("required_exclusion_classes") or []:
        if required not in classes:
            findings.append(f"exclusions: no exclusion of required class {required!r}")
    return claims


def _reconcile(
    row_claims: dict[str, list[str]],
    exclusion_claims: dict[str, list[str]],
    snapshot: dict[str, Any],
    findings: list[str],
) -> int:
    observed_raw = snapshot.get("observed")
    if not isinstance(observed_raw, list) or not observed_raw:
        findings.append("snapshot: observed must be a non-empty list")
        return 0
    observed = [str(token) for token in observed_raw]
    row_tokens = {token for tokens in row_claims.values() for token in tokens}
    exclusion_tokens = {
        token for tokens in exclusion_claims.values() for token in tokens
    }
    for token in sorted(set(observed) - row_tokens - exclusion_tokens):
        findings.append(
            f"unmatched: {token} is observed and is neither a row nor an exclusion"
        )
    for token in sorted(row_tokens & exclusion_tokens):
        findings.append(f"conflict: {token} is claimed by both a row and an exclusion")
    owners: dict[str, list[str]] = {}
    for row_id, tokens in row_claims.items():
        for token in tokens:
            owners.setdefault(token, []).append(row_id)
    for token, ids in sorted(owners.items()):
        if len(ids) > 1:
            findings.append(
                f"conflict: {token} is claimed by rows {', '.join(sorted(ids))}"
            )
    for row_id, tokens in sorted(row_claims.items()):
        for token in tokens:
            if token not in observed:
                findings.append(
                    f"stale: row {row_id} matches {token}, which the snapshot does not observe"
                )
    return len(observed)


def _load_mapping(
    path: Path, schema: str, label: str, findings: list[str]
) -> dict[str, Any]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        findings.append(f"{label}: cannot read {path.name}: {type(exc).__name__}")
        return {}
    if not isinstance(document, dict):
        findings.append(f"{label}: {path.name} is not a mapping")
        return {}
    if document.get("schema_version") != schema:
        findings.append(f"{label}: schema_version must be {schema}")
    return document


def resolve_inventory_path(request: ModelRuntimeInventoryRequest) -> Path | None:
    """Return the declared inventory file, or None when no input declares one."""
    if request.inventory_path:
        return Path(request.inventory_path)
    from_env = os.environ.get(INVENTORY_FILE_ENV, "").strip()
    if from_env:
        return Path(from_env)
    roots = request.overlay_roots or [
        part
        for part in os.environ.get(OVERLAY_ROOTS_ENV, "").split(os.pathsep)
        if part.strip()
    ]
    for root in roots:
        candidate = Path(root) / OVERLAY_DIR_NAME / INVENTORY_FILE_NAME
        if candidate.is_file():
            return candidate
    return None


class HandlerRuntimeInventory:
    """Checks a runtime inventory overlay against its recorded host snapshot."""

    def handle(
        self, request: ModelRuntimeInventoryRequest
    ) -> ModelRuntimeInventoryResult:
        findings: list[str] = []
        inventory_path = resolve_inventory_path(request)
        if inventory_path is None:
            return ModelRuntimeInventoryResult(
                verdict="FAIL",
                findings=[
                    "no inventory declared: set inventory_path, "
                    f"{INVENTORY_FILE_ENV} or an overlay root holding {OVERLAY_DIR_NAME}/{INVENTORY_FILE_NAME}"
                ],
            )
        inventory = _load_mapping(
            inventory_path, INVENTORY_SCHEMA, "inventory", findings
        )
        snapshot_name = inventory.get("snapshot_file")
        if request.snapshot_path:
            snapshot_path = Path(request.snapshot_path)
        elif isinstance(snapshot_name, str) and snapshot_name.strip():
            snapshot_path = inventory_path.parent / snapshot_name
        else:
            snapshot_path = inventory_path.parent / "<undeclared>"
            findings.append("inventory: snapshot_file is missing or blank")
        snapshot = _load_mapping(snapshot_path, SNAPSHOT_SCHEMA, "snapshot", findings)
        row_claims = _check_rows(inventory, findings)
        exclusion_claims = _check_exclusions(inventory, findings)
        observed_count = _reconcile(row_claims, exclusion_claims, snapshot, findings)
        return ModelRuntimeInventoryResult(
            verdict="FAIL" if findings else "PASS",
            row_count=len(row_claims),
            exclusion_count=len(exclusion_claims),
            observed_count=observed_count,
            inventory_path=str(inventory_path),
            snapshot_path=str(snapshot_path),
            findings=findings,
        )
