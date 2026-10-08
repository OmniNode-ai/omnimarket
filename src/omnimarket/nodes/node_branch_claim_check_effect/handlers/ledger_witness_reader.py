# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File witness for parity measurement, never for holder resolution."""

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_branch_claim_check_effect.models.model_branch_claim_policy import (
    ModelParityWitness,
)
from omnimarket.nodes.node_projection_work_ledger.parity import split_rows

_ARCHIVE_DATE_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})-split\.md$")


class ProtocolLedgerWitness(Protocol):
    def read_rows(self) -> list[str]: ...


class FileLedgerWitness:
    def __init__(self, source: ModelParityWitness, *, since: datetime) -> None:
        self._source = source
        self._since = since

    def read_rows(self) -> list[str]:
        value = os.environ.get(self._source.ledger_path_env)
        if not value or not value.strip():
            raise RuntimeError(
                f"contract-declared {self._source.ledger_path_env} is unset"
            )
        ledger = Path(value)
        archive = ledger.parent / self._source.archive_dir_name
        selected: list[tuple[str, Path]] = []
        # iterdir surfaces permission errors; Path.glob can hide them.
        if archive.exists():
            for path in archive.iterdir():
                match = _ARCHIVE_DATE_RE.search(path.name)
                if (
                    match
                    and datetime.strptime(match.group(1), "%Y-%m-%d").date()
                    >= self._since.date()
                ):
                    selected.append((match.group(1), path))
        rows: list[str] = []
        for path in [path for _, path in sorted(selected)] + [ledger]:
            rows.extend(split_rows(path.read_text(encoding="utf-8")))
        return rows
