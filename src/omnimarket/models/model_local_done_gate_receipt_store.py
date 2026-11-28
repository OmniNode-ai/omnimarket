# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a local Done gate's receipt for a skipped ``local_done_gate`` item.

The hosted verifier counts a skipped ``local_done_gate`` item as verified only
when a receipt for that ticket, that exact item and that exact contract revision
exists and its latest verdict reads VERIFIED. Anything else leaves the item
skipped, which the gate refuses.
"""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.model_dod_verify_state import ModelEvidenceCheckResult
from omnimarket.models.model_local_done_gate_receipt import (
    ModelLocalDoneGateItemVerdict,
    ModelLocalDoneGateReceipt,
)


class ProtocolLocalDoneGateReceiptSource(Protocol):
    """Receipts published on the bus by local Done gate runs, oldest first."""

    def receipts_for(self, ticket_id: str) -> list[ModelLocalDoneGateReceipt]: ...


class FilesystemLocalDoneGateReceiptStore:
    """Receipts the bus consumer persisted, one JSON line each, per ticket."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, ticket_id: str) -> Path:
        return self._root / f"{ticket_id}.jsonl"

    def append(self, receipt: ModelLocalDoneGateReceipt) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        with self._path(receipt.ticket_id).open("a", encoding="utf-8") as handle:
            handle.write(receipt.model_dump_json() + "\n")

    def receipts_for(self, ticket_id: str) -> list[ModelLocalDoneGateReceipt]:
        path = self._path(ticket_id)
        if not path.is_file():
            return []
        receipts: list[ModelLocalDoneGateReceipt] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                receipts.append(ModelLocalDoneGateReceipt.model_validate_json(line))
            except ValidationError:
                continue  # a malformed line is no receipt
        return receipts


def default_receipt_store() -> FilesystemLocalDoneGateReceiptStore | None:
    """The store under the evidence home, or None when no state root is set."""
    from omnimarket.nodes.node_dod_verify.services.evidence_home import (
        declared_evidence_home,
    )

    home = declared_evidence_home()
    if home.startswith("<"):
        return None
    return FilesystemLocalDoneGateReceiptStore(Path(home) / "local_done_gate_receipts")


def receipt_from_results(
    *,
    ticket_id: str,
    contract_sha: str | None,
    run_id: str,
    results: list[ModelEvidenceCheckResult],
    execution_audience: EnumDodVerifyExecutionAudience | None,
) -> ModelLocalDoneGateReceipt | None:
    """The receipt of a local_done_gate run; None for any other run or no revision."""
    if execution_audience is not EnumDodVerifyExecutionAudience.LOCAL_DONE_GATE:
        return None
    if contract_sha is None or not results:
        return None
    reportable = {"VERIFIED", "FAILED", "SKIPPED", "NON_PROBATIVE"}
    items = tuple(
        ModelLocalDoneGateItemVerdict(item_id=r.evidence_id, verdict=r.status.name)
        for r in results
        if not r.is_disposition and r.status.name in reportable
    )
    return ModelLocalDoneGateReceipt(
        ticket_id=ticket_id,
        contract_sha=contract_sha,
        items=items,
        host=socket.gethostname(),
        run_id=run_id,
    )


def receipt_reads_verified(
    receipts: list[ModelLocalDoneGateReceipt],
    *,
    ticket_id: str,
    contract_sha: str | None,
    item_id: str,
) -> ModelLocalDoneGateReceipt | None:
    """The latest matching receipt when it reads VERIFIED for the item, else None.

    Matching is exact: the ticket, the contract revision and the item id. A later
    receipt that reads the item otherwise supersedes an earlier VERIFIED one.
    """
    if contract_sha is None:
        return None
    latest: tuple[ModelLocalDoneGateReceipt, str] | None = None
    for receipt in receipts:
        if receipt.ticket_id != ticket_id or receipt.contract_sha != contract_sha:
            continue
        for item in receipt.items:
            if item.item_id == item_id:
                latest = (receipt, item.verdict)
    if latest is not None and latest[1] == "VERIFIED":
        return latest[0]
    return None


__all__ = [
    "FilesystemLocalDoneGateReceiptStore",
    "ProtocolLocalDoneGateReceiptSource",
    "default_receipt_store",
    "receipt_from_results",
    "receipt_reads_verified",
]
