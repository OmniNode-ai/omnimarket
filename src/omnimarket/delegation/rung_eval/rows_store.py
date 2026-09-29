# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic per-night JSONL storage."""

from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from omnimarket.delegation.rung_eval.models import ModelRungEvalRow


def write_night(directory: Path, night: date, rows: list[ModelRungEvalRow]) -> Path:
    if any(row.night != night for row in rows):
        raise ValueError("All rows must belong to the requested night")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"rung_eval_{night.isoformat()}.jsonl"
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory, suffix=".tmp", delete=False
        ) as handle:
            temporary = Path(handle.name)
            for row in rows:
                handle.write(row.model_dump_json() + "\n")
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def read_rows(directory: Path) -> list[ModelRungEvalRow]:
    return [
        ModelRungEvalRow.model_validate_json(line)
        for path in sorted(directory.glob("rung_eval_*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
