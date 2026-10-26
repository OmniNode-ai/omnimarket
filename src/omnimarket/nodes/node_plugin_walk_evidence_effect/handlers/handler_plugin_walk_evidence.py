# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grade captured plugin walks and read their receipts from the real local store.

Claude's exit status and prose are deliberately not acceptance authorities.
The hosted tests replay reduced, recorded stream events and store rows; a lab
invocation reads the original streams and SQLite store with this same grader.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

from omnimarket.nodes.node_plugin_walk_evidence_effect.models.model_plugin_walk_evidence import (
    ModelPluginWalkEvidenceRequest,
    ModelPluginWalkEvidenceResult,
)


def _init(events: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [
        e for e in events if e.get("type") == "system" and e.get("subtype") == "init"
    ]
    if len(rows) != 1:
        raise ValueError("walk must carry exactly one init event")
    return rows[0]


def delegate_receipts(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read delegate JSON only from Bash tool results, never assistant prose."""
    commands: set[str] = set()
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if (
                block.get("type") == "tool_use"
                and block.get("name") == "Bash"
                and str(block.get("input", {}).get("command", "")).startswith(
                    "onex delegate "
                )
            ):
                commands.add(block["id"])
    receipts = []
    for event in events:
        if event.get("type") != "user":
            continue
        for block in event.get("message", {}).get("content", []):
            if (
                block.get("type") != "tool_result"
                or block.get("tool_use_id") not in commands
            ):
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                content = "\n".join(part.get("text", "") for part in content)
            for line in str(content).splitlines():
                try:
                    receipt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(receipt, dict)
                    and receipt.get("node_name") == "node_delegate_skill_orchestrator"
                    and receipt.get("correlation_id")
                ):
                    UUID(receipt["correlation_id"])
                    receipts.append(receipt)
    return receipts


def grade_positive(events: list[dict[str, Any]], readback: dict[str, Any]) -> list[str]:
    init = _init(events)
    if init.get("skills", []).count("onex:delegate") != 1:
        raise ValueError("positive walk must list onex:delegate exactly once")
    receipts = delegate_receipts(events)
    if not receipts:
        raise ValueError(
            "positive walk has no onex delegate tool-result correlation id"
        )
    ids = []
    for receipt in receipts:
        cid = receipt["correlation_id"]
        rows = readback.get(cid, {})
        delegations = rows.get("delegation_events", [])
        metrics = rows.get("llm_call_metrics", [])
        if len(delegations) != 1 or len(metrics) != 1:
            raise ValueError(
                "each correlation id must have exactly one row in each store table"
            )
        if delegations[0].get("terminal_ok") != 1:
            raise ValueError("store terminal_ok must be 1")
        if metrics[0].get("total_tokens", 0) <= 0:
            raise ValueError("store tokens must be nonzero")
        ids.append(cid)
    return ids


def grade_negative(events: list[dict[str, Any]]) -> None:
    if "onex:delegate" in _init(events).get("skills", []):
        raise ValueError("negative walk lists onex:delegate")
    if delegate_receipts(events):
        raise ValueError("negative walk has an onex delegate correlation id")


def _events(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class HandlerPluginWalkEvidence:
    """Read captured streams and query the original store without modifying it."""

    def handle(
        self, request: ModelPluginWalkEvidenceRequest
    ) -> ModelPluginWalkEvidenceResult:
        positive = _events(request.positive)
        readback = {}
        # Read-only: the acceptance check must never repair or seed its own evidence.
        with sqlite3.connect(
            request.database.resolve().as_uri() + "?mode=ro", uri=True
        ) as conn:
            conn.row_factory = sqlite3.Row
            for receipt in delegate_receipts(positive):
                cid = receipt["correlation_id"]
                readback[cid] = {
                    "delegation_events": [
                        dict(row)
                        for row in conn.execute(
                            "SELECT terminal_ok FROM delegation_events WHERE correlation_id=?",
                            (cid,),
                        )
                    ],
                    "llm_call_metrics": [
                        dict(row)
                        for row in conn.execute(
                            "SELECT total_tokens FROM llm_call_metrics WHERE correlation_id=?",
                            (cid,),
                        )
                    ],
                }
        ids = grade_positive(positive, readback)
        grade_negative(_events(request.negative))
        return ModelPluginWalkEvidenceResult(
            positive_correlation_ids=tuple(ids), readback=readback
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--positive", type=Path, required=True)
    parser.add_argument("--negative", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    result = HandlerPluginWalkEvidence().handle(
        ModelPluginWalkEvidenceRequest(
            positive=args.positive,
            negative=args.negative,
            database=args.database,
        )
    )
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0
