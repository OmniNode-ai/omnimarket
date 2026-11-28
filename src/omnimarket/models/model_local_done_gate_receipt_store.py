# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read a local Done gate's receipt for a skipped ``local_done_gate`` item.

The hosted verifier counts a skipped ``local_done_gate`` item as verified only
when a receipt for that ticket, that exact item and that exact contract revision
exists and a host's latest verdict reads VERIFIED. Anything else leaves the item
skipped, which the gate refuses.
"""

from __future__ import annotations

import os
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


class ChainedLocalDoneGateReceiptSource:
    """Receipts read from several stores in order, earlier stores first."""

    def __init__(self, sources: list[FilesystemLocalDoneGateReceiptStore]) -> None:
        self._sources = sources

    def receipts_for(self, ticket_id: str) -> list[ModelLocalDoneGateReceipt]:
        return [r for source in self._sources for r in source.receipts_for(ticket_id)]


def default_receipt_source() -> ChainedLocalDoneGateReceiptSource | None:
    """Every store the declared state roots name, or None when none is declared.

    The receipt consumer writes under the runtime's state root. A verifier the
    Done-write guard starts as ``skill dod_verify --state-root <temp dir>`` has
    ``ONEX_STATE_DIR`` pointed at that temp dir, while ``ONEX_STATE_ROOT`` keeps
    the runtime's own root. Reading only the first declared root reads an empty
    store, so each distinct declared root is read.
    """
    from omnimarket.config.state_root import ONEX_STATE_ROOT_ENV_KEYS
    from omnimarket.handlers.runtime_paths import runtime_path

    subdir = runtime_path("dod_evidence.home_subdir")
    roots: list[Path] = []
    for key in ONEX_STATE_ROOT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value and Path(value) not in roots:
            roots.append(Path(value))
    if not roots:
        return None
    return ChainedLocalDoneGateReceiptSource(
        [
            FilesystemLocalDoneGateReceiptStore(
                root / subdir / "local_done_gate_receipts"
            )
            for root in roots
        ]
    )


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
    """The latest matching receipt when a host's latest reading is VERIFIED, else None.

    Matching is exact: the ticket, the contract revision and the item id. Each
    host's own latest reading of the item stands for that host: a later receipt
    from the same host that reads the item otherwise supersedes its earlier
    VERIFIED one, but a FAILED reading from another host does not veto it. The
    item is lab-only, so a host that cannot reach the lab reads it FAILED while a
    lab host reads it VERIFIED, and the store's append order is the order the bus
    delivered the receipts in, not the order the runs happened in (OMN-26729).
    """
    if contract_sha is None:
        return None
    latest_by_host: dict[str, tuple[ModelLocalDoneGateReceipt, str]] = {}
    for receipt in receipts:
        if receipt.ticket_id != ticket_id or receipt.contract_sha != contract_sha:
            continue
        for item in receipt.items:
            if item.item_id == item_id:
                latest_by_host[receipt.host] = (receipt, item.verdict)
    verified = [r for r, verdict in latest_by_host.values() if verdict == "VERIFIED"]
    return verified[-1] if verified else None


__all__ = [
    "ChainedLocalDoneGateReceiptSource",
    "FilesystemLocalDoneGateReceiptStore",
    "ProtocolLocalDoneGateReceiptSource",
    "default_receipt_source",
    "default_receipt_store",
    "receipt_from_results",
    "receipt_reads_verified",
]
