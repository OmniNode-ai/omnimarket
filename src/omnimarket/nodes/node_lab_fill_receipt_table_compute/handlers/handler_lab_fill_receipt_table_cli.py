# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""`python -m omnimarket.nodes.node_lab_fill_receipt_table_compute`: lane receipts in as JSON, the table and retries out (OMN-17427).

The JSON is {"lanes": [{lane, ticket, model, planned_host, pinned, detached, skipped, detail,
receipt}], "admitting_hosts": [{name, free_lanes, engines}]}, from --json or standard input. It
prints one JSON object (rows, table, counts, retries); --table prints only the markdown table,
which is what the throughput tick's dispatch lines embed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from ..models import (
    ModelAdmittingHost,
    ModelLaneReceiptInput,
    ModelLaneReceiptTableRequest,
    ModelLaneReceiptTableResult,
)
from .handler_lab_fill_receipt_table import HandlerLabFillReceiptTable


def request_from_json(text: str) -> ModelLaneReceiptTableRequest:
    """Parse the CLI input; a malformed document raises ValueError naming the field."""
    data: Any = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("lanes"), list):
        raise ValueError("input must be an object with a lanes array")
    lanes = []
    for raw in data["lanes"]:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("lane"), str)
            or not raw["lane"]
        ):
            raise ValueError("every lane needs a lane name")
        receipt = raw.get("receipt")
        if receipt is not None and not isinstance(receipt, dict):
            raise ValueError(f"lane {raw['lane']}: receipt must be an object or null")
        lanes.append(
            ModelLaneReceiptInput(
                lane=raw["lane"],
                ticket=str(raw.get("ticket") or ""),
                model=str(raw.get("model") or ""),
                planned_host=str(raw.get("planned_host") or ""),
                pinned=raw.get("pinned") is True,
                detached=raw.get("detached", True) is not False,
                skipped=str(raw.get("skipped") or ""),
                detail=str(raw.get("detail") or ""),
                receipt=receipt,
            )
        )
    hosts = tuple(
        ModelAdmittingHost(
            str(h["name"]),
            int(h.get("free_lanes", 0)),
            tuple(str(e) for e in h.get("engines", ())),
        )
        for h in data.get("admitting_hosts", [])
    )
    return ModelLaneReceiptTableRequest(lanes=tuple(lanes), admitting_hosts=hosts)


def result_to_json(result: ModelLaneReceiptTableResult) -> dict[str, Any]:
    """The result as plain JSON: enums by value, counts as an object."""
    return {
        "rows": [
            {
                **asdict(row),
                "state": row.state.value,
                "refusal_class": row.refusal_class.value,
            }
            for row in result.rows
        ],
        "table": result.table,
        "counts": dict(result.counts),
        "retries": [asdict(retry) for retry in result.retries],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omnimarket.nodes.node_lab_fill_receipt_table_compute",
        description=__doc__,
    )
    parser.add_argument("--json", help="the request document (default: standard input)")
    parser.add_argument(
        "--table", action="store_true", help="print only the markdown table"
    )
    args = parser.parse_args(argv)
    try:
        request = request_from_json(
            args.json if args.json is not None else sys.stdin.read()
        )
    except ValueError as exc:
        sys.stderr.write(f"lab-fill-receipt-table: refused: {exc}\n")
        return 2
    result = HandlerLabFillReceiptTable().handle(request)
    sys.stdout.write(
        (
            result.table
            if args.table
            else json.dumps(result_to_json(result), sort_keys=True)
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
