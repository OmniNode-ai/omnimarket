# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seven-day count of accepted code answers governed by a response contract.

Source: the delegation run store, ``<state root>/runs/<run>/receipt.json`` and
``run.json``. A run counts when its receipt is newer than the window, its task
type is ``code_generation``, its status is ``success`` and its receipt carries
``response_contract_evidence``.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

WINDOW_SECONDS = 7 * 86400


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {}


def count_governed_code_answers(
    runs_dir: Path, *, now: float | None = None, window_seconds: int = WINDOW_SECONDS
) -> int:
    """Count accepted code answers in the window that carry contract evidence."""
    current = time.time() if now is None else now
    total = 0
    for run in sorted(runs_dir.glob("*")):
        receipt_path = run / "receipt.json"
        if not receipt_path.exists():
            continue
        if current - receipt_path.stat().st_mtime >= window_seconds:
            continue
        receipt = _read(receipt_path)
        if (
            _read(run / "run.json").get("task_type") == "code_generation"
            and receipt.get("status") == "success"
            and receipt.get("response_contract_evidence")
        ):
            total += 1
    return total


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: governed_code_count <runs directory>\n")
        return 2
    runs_dir = Path(args[0])
    count = count_governed_code_answers(runs_dir)
    sys.stdout.write(f"accepted_code_governed_by_contract={count}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
