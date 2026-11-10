# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DirectoryReceiptStore: the lab proof judge's substitute-receipt store on disk.

Receipts live under ``<root>/<subdir>/<node>/*.receipt.json``. ``read`` returns
the receipts for a (repo, node) pair; ``write`` persists one receipt and returns
its path. The store is the CLI's only I/O: the pure handlers read no disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnimarket.nodes.node_lab_proof_judge_compute.models.model_substitute_receipt import (
    ModelSubstituteReceipt,
)


def _load(path: Path) -> ModelSubstituteReceipt:
    raw = path.read_text()  # node-purity-ok: OMN-23642 disk boundary
    return ModelSubstituteReceipt.model_validate(json.loads(raw))


class DirectoryReceiptStore:
    """Read and write substitute receipts from a directory tree."""

    def __init__(self, root: Path, subdir: str) -> None:
        self._root = root
        self._subdir = subdir

    def _node_dir(self, node: str) -> Path:
        return self._root / self._subdir / node

    def read(self, repo: str, node: str) -> tuple[ModelSubstituteReceipt, ...]:
        """Return the receipts for ``repo`` and ``node`` (empty when absent)."""
        directory = self._node_dir(node)
        if not directory.is_dir():
            return ()
        receipts: list[ModelSubstituteReceipt] = []
        for path in sorted(directory.glob("*.receipt.json")):
            receipt = _load(path)
            if receipt.repo == repo:
                receipts.append(receipt)
        return tuple(receipts)

    def read_for_head(self, repo: str, head: str) -> tuple[ModelSubstituteReceipt, ...]:
        """Every receipt of ``repo`` at exactly ``head``, over all node directories (empty when none)."""
        base = self._root / self._subdir
        if not base.is_dir():
            return ()
        found: list[ModelSubstituteReceipt] = []
        for path in sorted(base.glob("*/*.receipt.json")):
            receipt = _load(path)
            if receipt.repo == repo and receipt.head == head:
                found.append(receipt)
        return tuple(found)

    def write(self, receipt: ModelSubstituteReceipt) -> str:
        """Persist ``receipt`` and return its path."""
        directory = self._node_dir(receipt.node)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{receipt.receipt_id}.receipt.json"
        body = receipt.model_dump_json(indent=2) + "\n"
        path.write_text(body)  # node-purity-ok: OMN-23642 disk boundary
        return str(path)
