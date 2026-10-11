# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ledger rows the operator capture needs: rulings, captures and closures.

The live ledger and its roll archives (``archive/*.md`` beside it) are scanned line by line and
only rows that can matter are kept: RULING rows, ``operator-capture`` rows and rows citing
``closes-ask=``. A missing archive directory is not an error; a missing ledger is.
"""

from __future__ import annotations

from pathlib import Path

NEEDLES = ("| RULING |", "lane=operator-capture", "closes-ask=")


def relevant_rows(ledger_path: Path, *, archives: bool = True) -> tuple[str, ...]:
    paths: list[Path] = []
    if archives:
        archive_dir = ledger_path.parent / "archive"
        if archive_dir.is_dir():
            paths.extend(sorted(archive_dir.glob("*.md")))
    paths.append(ledger_path)
    rows: list[str] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            rows.extend(
                line.rstrip("\n") for line in handle if any(n in line for n in NEEDLES)
            )
    return tuple(rows)


__all__ = ["NEEDLES", "relevant_rows"]
