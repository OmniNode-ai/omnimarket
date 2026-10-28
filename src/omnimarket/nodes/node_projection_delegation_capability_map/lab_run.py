# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed lab command for the delegation capability map (OMN-20169, RS.5).

``seed-labelled`` reads one rater's labelled items (``delegation_eval_items``)
with the model and backend that answered each (``delegation_events``), re-bands
every stored prompt snapshot with the shared size-band measurement, and hands the map run
to the rule-7a writer of this node, the same writer the runtime dispatches in
process. The labelled items were answered under the default configuration
(full context, provider defaults), so that is the configuration of every row.

Each row's six identity fields are the answering model (model revision), its
backend, the configuration id, the rubric version (suite), the rater role
(grader) and ``delegation_eval_items`` (harness). A row whose event names no
model or backend lacks a field and is refused by the fold, counted by reason.
The map run id is a uuid5 of the label set, so a re-run of the same labels is
the same run and writes no new row. Prints a content-free JSON receipt.

DSNs come from environment variables named on the command line, with no default
(root rule 8): ``--read-dsn-env`` reads the two tables, ``--write-dsn-env`` is
the tenant projection writer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.handlers.handler_size_band_measurement import measure_size_band
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)
from omnimarket.lab_work.named_env import read_named_env
from omnimarket.models.delegation.model_size_band import ModelSizeBand
from omnimarket.models.delegation_capability_map.enum_capability_source import (
    EnumCapabilitySource,
)
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_identity import (
    ModelCapabilityIdentity,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_delegation_capability_map_writer import (
    HandlerDelegationCapabilityMapWriter,
)

HARNESS_REVISION = "delegation_eval_items"
_LABELS = {"adequate": True, "inadequate": False}

_LABELLED_SQL = """
    SELECT i.item_key, i.task_class, i.label, i.prompt_snapshot, i.observed_at,
           i.rubric_version, i.rater_role,
           NULLIF(COALESCE(e.routed_model, e.model_name), '') AS model_key,
           NULLIF(e.backend_id, '') AS backend_id,
           NULLIF(e.host, '') AS host,
           NULLIF(e.answering_backend, '') AS answering_backend
    FROM public.delegation_eval_items AS i
    LEFT JOIN public.delegation_events AS e
      ON e.correlation_id = i.correlation_id AND e.tenant_id::text = i.tenant_id::text
    WHERE i.tenant_id = $1::uuid AND i.rater_role = $2 AND i.rubric_version = $3
"""


def _band(
    authority: ModelTaskClassAuthority, task_class: str, prompt: str
) -> ModelSizeBand | str:
    """The measured band of one stored prompt, or why it cannot be measured."""
    if task_class not in authority.task_classes:
        return "task_class_unavailable"
    resolved = authority.size_band_thresholds_for(task_class)
    if resolved is None:
        return "size_thresholds_unavailable"
    thresholds, reference = resolved
    return measure_size_band(
        task_class=task_class,
        prompt=prompt,
        context_pack="",
        sources=(),
        acceptance_criteria=(),
        thresholds=thresholds,
        thresholds_reference=reference,
    )


def build_map_run(
    tenant_id: UUID,
    items: Sequence[dict[str, Any]],
    authority: ModelTaskClassAuthority | None = None,
) -> tuple[ModelCapabilityMapRunRecorded, dict[str, int]]:
    """One map run from labelled items, and the count of items it could not use."""
    classes = authority or load_task_class_authority()
    configuration = ModelCapabilityConfiguration()
    skipped: Counter[str] = Counter()
    rows: list[ModelCapabilityVerdictRow] = []
    for item in items:
        passed = _LABELS.get(str(item.get("label") or ""))
        if passed is None:
            skipped["label_not_adequate_or_inadequate"] += 1
            continue
        band = _band(
            classes,
            str(item.get("task_class") or "unknown"),
            str(item.get("prompt_snapshot") or ""),
        )
        if not isinstance(band, ModelSizeBand):
            skipped[f"size_band_{band}"] += 1
            continue
        model_key = item.get("model_key")
        backend_id = item.get("backend_id")
        rows.append(
            ModelCapabilityVerdictRow(
                tenant_id=tenant_id,
                item_key=str(item["item_key"]),
                source=EnumCapabilitySource.LABELLED_ITEM,
                task_class=band.task_class,
                size_band=band.band,
                model_key=str(model_key or "unrecorded"),
                backend_id=str(backend_id or "unrecorded"),
                configuration=configuration,
                identity=ModelCapabilityIdentity(
                    model_revision=model_key or None,
                    backend=backend_id or None,
                    inference_configuration=configuration.configuration_id,
                    suite_revision=str(item["rubric_version"]),
                    grader_revision=str(item["rater_role"]),
                    harness_revision=HARNESS_REVISION,
                ),
                passed=passed,
                host=item.get("host") or None,
                answering_backend=item.get("answering_backend") or None,
            )
        )
    assignments = {
        (row.model_key, row.backend_id): ModelCapabilityServingAssignment(
            model_key=row.model_key,
            backend_id=row.backend_id,
            configuration_id=configuration.configuration_id,
            identity=row.identity,
        )
        for row in rows
        if not row.identity.missing_fields()
    }
    digest = hashlib.sha256(
        json.dumps(
            sorted(
                (r.item_key, r.model_key, r.backend_id, r.task_class, r.passed)
                for r in rows
            )
        ).encode("utf-8")
    ).hexdigest()
    observed = [
        item["observed_at"]
        for item in items
        if isinstance(item.get("observed_at"), datetime)
    ]
    if not observed:
        raise SystemExit("no labelled item carries an observed_at; nothing to seed")
    run = ModelCapabilityMapRunRecorded(
        tenant_id=tenant_id,
        map_run_id=str(uuid5(NAMESPACE_URL, f"omn-20169:labelled:{digest}")),
        observed_at=max(observed),
        rows=tuple(rows),
        assignments=tuple(assignments[key] for key in sorted(assignments)),
    )
    return run, dict(sorted(skipped.items()))


async def _read_labelled(args: argparse.Namespace, tenant: str) -> list[dict[str, Any]]:
    reader = AsyncpgAdapter(dsn=read_named_env(args.read_dsn_env))
    await reader.connect()
    try:
        return await reader.execute(
            _LABELLED_SQL, tenant, args.rater_role, args.rubric_version, tenant=tenant
        )
    finally:
        await reader.close()


def _seed_labelled(args: argparse.Namespace) -> dict[str, Any]:
    tenant = UUID(args.tenant_id)
    items = asyncio.run(_read_labelled(args, str(tenant)))
    run, skipped = build_map_run(tenant, items)
    writer = HandlerDelegationCapabilityMapWriter()
    writer.bind_projection_database_url(read_named_env(args.write_dsn_env))
    applied = writer.handle(run.model_dump(mode="json"))
    return {
        "map_run_id": run.map_run_id,
        "labelled_items": len(items),
        "verdict_rows": len(run.rows),
        "items_skipped": skipped,
        "writer": applied,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delegation-capability-map-lab-run")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--read-dsn-env", required=True)
    parser.add_argument("--write-dsn-env", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed-labelled")
    seed.add_argument("--rater-role", required=True)
    seed.add_argument("--rubric-version", required=True)
    args = parser.parse_args(argv)
    receipt = _seed_labelled(args)
    json.dump(receipt, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
