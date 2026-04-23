# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ReceiptWriter — persists ModelDodReportReceipt to disk.

Construction:
    writer = ReceiptWriter.from_env()   # reads ONEX_EVIDENCE_ROOT; raises KeyError if unset
    writer = ReceiptWriter(config)      # inject a pre-built config (tests / DI)

Write path:
    - If ``output_path`` is provided to ``write()``, uses that path directly.
    - Otherwise resolves to ``{evidence_root}/{ticket_id}/dod_report.json``.

Writes are atomic: the JSON is written to a ``.tmp`` sibling then renamed, so
the hook never sees a partial file.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dod_verify.models.model_dod_report_receipt import (
    ModelDodReportReceipt,
)


class ModelReceiptWriterConfig(BaseModel):
    """Configuration for ReceiptWriter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_root: Path = Field(
        ..., description="Root directory for evidence receipts (ONEX_EVIDENCE_ROOT)."
    )
    atomic_write: bool = Field(
        default=True,
        description="Write to a .tmp file then rename (prevents partial reads).",
    )
    mkdir_parents: bool = Field(
        default=True,
        description="Create parent directories if they do not exist.",
    )


class ReceiptWriter:
    """Writes ModelDodReportReceipt to disk.

    Usage::

        writer = ReceiptWriter.from_env()
        path = writer.write(receipt)          # resolves path from receipt.ticket_id
        path = writer.write(receipt, output_path=Path("/explicit/path.json"))
    """

    def __init__(self, config: ModelReceiptWriterConfig) -> None:
        self._config = config

    @classmethod
    def from_env(cls) -> ReceiptWriter:
        """Construct from environment variables.

        Reads ONEX_EVIDENCE_ROOT and fails fast (KeyError) if unset —
        per omni_home/CLAUDE.md §8 (fail-fast on missing env, not silent fallback).
        """
        evidence_root = Path(os.environ["ONEX_EVIDENCE_ROOT"])
        config = ModelReceiptWriterConfig(evidence_root=evidence_root)
        return cls(config)

    def resolve_path(self, ticket_id: str) -> Path:
        """Return the canonical receipt path for a ticket ID."""
        return self._config.evidence_root / ticket_id / "dod_report.json"

    def write(
        self,
        receipt: ModelDodReportReceipt,
        output_path: Path | None = None,
    ) -> Path:
        """Serialize receipt to disk and return the written path.

        Args:
            receipt: The receipt model to persist.
            output_path: Explicit destination path.  If None, resolves to
                ``{evidence_root}/{ticket_id}/dod_report.json``.

        Returns:
            The path to which the receipt was written.

        Raises:
            OSError: If the directory cannot be created or the file cannot be written.
        """
        target = (
            output_path
            if output_path is not None
            else self.resolve_path(receipt.ticket_id)
        )

        if self._config.mkdir_parents:
            target.parent.mkdir(parents=True, exist_ok=True)

        json_bytes = receipt.model_dump_json(indent=2).encode("utf-8")

        if self._config.atomic_write:
            # Write to a .tmp sibling, then atomically rename.
            # mkstemp produces 0600 by default; chmod to 0o644 before rename so
            # downstream audit tools / hooks running as the same user (or group)
            # can read the file without elevated permissions.
            fd, tmp_path_str = tempfile.mkstemp(
                dir=target.parent, prefix=".dod_report_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(json_bytes)
                os.chmod(tmp_path_str, 0o644)
                Path(tmp_path_str).replace(target)
            except Exception:
                # Clean up tmp file on error; re-raise
                with contextlib.suppress(OSError):
                    Path(tmp_path_str).unlink(missing_ok=True)
                raise
        else:
            target.write_bytes(json_bytes)

        return target


__all__: list[str] = ["ModelReceiptWriterConfig", "ReceiptWriter"]
