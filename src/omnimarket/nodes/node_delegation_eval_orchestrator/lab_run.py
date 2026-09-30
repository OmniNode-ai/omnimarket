# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed lab command for the delegation eval (OMN-19793, EV.4).

Runs the two orchestrator operations in process against a lab database and
hands each payload to the rule-7a writer of ``node_projection_delegation_eval``,
the same writer the runtime dispatches in process. The bus leg (command topic in,
event topic out) is the runtime's; this command exists so a lab run is one
committed, reproducible invocation.

Two subcommands:

``label-record --labels <jsonl>``
    Each line: ``{"correlation_id", "attempt_index", "label", "stratum",
    "computed_facts"}``. ``computed_facts.manifest_id`` names the manifest the
    item belongs to (the run operation selects on it). The snapshot comes from
    ``delegation_events`` under the tenant, scrubbed by the label-record handler.

``run --manifest-id <id> --gate-version <v>``
    Replays one rater's labels of the manifest and writes the verdict and
    results rows under the eval run id. Prints a content-free JSON receipt.

DSNs come from environment variables named on the command line, with no default
(root rule 8): ``--read-dsn-env`` reads ``delegation_events`` and
``delegation_eval_items``; ``--write-dsn-env`` is the tenant projection writer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from importlib import metadata
from pathlib import Path
from typing import Any
from uuid import UUID

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.events.delegation_eval import (
    ModelDelegationEvalRunRequest,
    ModelLabelRecordRequest,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.handlers.handler_delegation_eval_orchestrator import (
    HandlerDelegationEvalOrchestrator,
)
from omnimarket.nodes.node_delegation_eval_orchestrator.models import (
    ModelDelegationEventSnapshot,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_eval_label import (
    EnumGateEvalLabel,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.enum_gate_verdict import (
    EnumGateVerdict,
)
from omnimarket.nodes.node_delegation_gate_eval_compute.models.model_gate_eval_item import (
    ModelGateEvalItem,
)
from omnimarket.nodes.node_projection_delegation_eval.handlers.handler_delegation_eval_writer import (
    DelegationEvalProjectionWriter,
)

_SNAPSHOT_SQL = """
    SELECT correlation_id, prompt_text, response_text, task_type,
           quality_gate_passed, quality_gate_detail, quality_gates_failed_jsonb
    FROM public.delegation_events
    WHERE tenant_id = $1::uuid AND correlation_id = ANY($2::text[])
"""

_LABELLED_ITEMS_SQL = """
    SELECT item_key, task_class, stratum, label, prompt_snapshot,
           response_snapshot, gate_verdict, deciding_check
    FROM public.delegation_eval_items
    WHERE tenant_id = $1::uuid AND rater_role = $2 AND rubric_version = $3
      AND computed_facts ->> 'manifest_id' = $4
"""

_VERDICTS = {"accepted": EnumGateVerdict.ACCEPTED, "refused": EnumGateVerdict.REFUSED}


def _deciding_check(row: dict[str, Any]) -> str:
    failed = row.get("quality_gates_failed_jsonb")
    if isinstance(failed, str):
        failed = json.loads(failed)
    if isinstance(failed, list) and failed:
        return ",".join(str(check) for check in failed)
    return str(row.get("quality_gate_detail") or "")[:200]


class _PrefetchedSnapshots:
    """Sync snapshot port over rows read before the handler runs."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self._rows = rows

    def get_snapshot(
        self, correlation_id: str, attempt_index: int
    ) -> ModelDelegationEventSnapshot:
        row = self._rows[correlation_id]
        return ModelDelegationEventSnapshot(
            prompt=row["prompt_text"] or "",
            response=row["response_text"] or "",
            task_class=row["task_type"] or "",
            gate_verdict="accepted" if row["quality_gate_passed"] else "refused",
            deciding_check=_deciding_check(row),
        )


class _PrefetchedLabelledItems:
    """Sync labelled-item port over rows read before the handler runs."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get_labelled_items(
        self, manifest_id: str, rater_role: str, rubric_version: str
    ) -> tuple[ModelGateEvalItem, ...]:
        return tuple(
            ModelGateEvalItem(
                item_id=row["item_key"],
                task_class=row["task_class"] or "",
                stratum=row["stratum"] or "",
                label=EnumGateEvalLabel(row["label"]),
                prompt_text=row["prompt_snapshot"] or "",
                recorded_answer=row["response_snapshot"],
                recorded_verdict=_VERDICTS.get(row["gate_verdict"] or ""),
                recorded_deciding_check=row["deciding_check"] or None,
            )
            for row in self._rows
        )


def _dsn(env_name: str) -> str:
    return os.environ[env_name]


async def _read_snapshots(
    args: argparse.Namespace, tenant: str, ids: list[str]
) -> list[dict[str, Any]]:
    reader = AsyncpgAdapter(dsn=_dsn(args.read_dsn_env))
    await reader.connect()
    try:
        return await reader.execute(_SNAPSHOT_SQL, tenant, ids, tenant=tenant)
    finally:
        await reader.close()


def _label_record(args: argparse.Namespace) -> dict[str, Any]:
    tenant = str(UUID(args.tenant_id))
    labels = [
        ModelLabelRecordRequest.model_validate(
            {"tenant_id": tenant, **json.loads(line)}
        )
        for line in Path(args.labels).read_text().splitlines()
        if line.strip()
    ]
    rows = asyncio.run(
        _read_snapshots(args, tenant, [label.correlation_id for label in labels])
    )
    by_id = {row["correlation_id"]: row for row in rows}
    missing = sorted({label.correlation_id for label in labels} - set(by_id))
    if missing:
        raise SystemExit(
            f"no delegation_events row for {len(missing)} ids: {missing[:5]}"
        )
    handler = HandlerDelegationEvalOrchestrator(_PrefetchedSnapshots(by_id))
    writer = DelegationEvalProjectionWriter()
    writer.bind_projection_database_url(_dsn(args.write_dsn_env))
    written = 0
    for label in labels:
        payload = handler.handle(label).payload.model_dump(mode="json")
        written += int(writer.handle(payload)["rows_upserted"])
    return {"labels": len(labels), "rows_upserted": written}


async def _read_labelled(args: argparse.Namespace, tenant: str) -> list[dict[str, Any]]:
    reader = AsyncpgAdapter(dsn=_dsn(args.read_dsn_env))
    await reader.connect()
    try:
        return await reader.execute(
            _LABELLED_ITEMS_SQL,
            tenant,
            args.rater_role,
            args.rubric_version,
            args.manifest_id,
            tenant=tenant,
        )
    finally:
        await reader.close()


def _run(args: argparse.Namespace) -> dict[str, Any]:
    tenant = str(UUID(args.tenant_id))
    rows = asyncio.run(_read_labelled(args, tenant))
    gate_version = args.gate_version or f"omnimarket=={metadata.version('omnimarket')}"
    result = HandlerDelegationEvalRun(_PrefetchedLabelledItems(rows)).handle(
        ModelDelegationEvalRunRequest(
            tenant_id=UUID(tenant),
            manifest_id=args.manifest_id,
            rater_role=args.rater_role,
            rubric_version=args.rubric_version,
            gate_version=gate_version,
        )
    )
    payload = result.payload.model_dump(mode="json")
    writer = DelegationEvalProjectionWriter()
    writer.bind_projection_database_url(_dsn(args.write_dsn_env))
    applied = writer.handle(payload)
    return {
        "eval_run_id": payload["eval_run_id"],
        "status": payload["status"],
        "failure_reasons": payload["failure_reasons"],
        "labelled_items": len(rows),
        "label_set_sha256": payload["label_set_sha256"],
        "gate_version": gate_version,
        "writer": applied,
        "results": payload["results"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delegation-eval-lab-run")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--read-dsn-env", required=True)
    parser.add_argument("--write-dsn-env", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("label-record")
    record.add_argument("--labels", required=True)
    run = sub.add_parser("run")
    run.add_argument("--manifest-id", required=True)
    run.add_argument("--rater-role", required=True)
    run.add_argument("--rubric-version", required=True)
    run.add_argument("--gate-version", default="")
    args = parser.parse_args(argv)
    receipt = _label_record(args) if args.command == "label-record" else _run(args)
    json.dump(receipt, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if receipt.get("status", "completed") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
