# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The mechanize brief stays bounded however many rows its class holds.

The recorded case: exception-queue mechanize briefs reached 455 KB, because every row of the class
was pasted verbatim. The rows below are invented fixtures of that shape.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnimarket.nodes.node_exception_queue_effect import handler as exception_queue

pytestmark = pytest.mark.unit


def _rows(count: int, evidence_chars: int) -> list[dict[str, str]]:
    return [
        {
            "repo": "repo-a",
            "id": f"i{n}",
            "key": f"k{n}",
            "source": "s",
            "evidence": f"row{n}:" + "e" * evidence_chars,
            "tried": "t",
            "proposed_fix": "p",
        }
        for n in range(count)
    ]


def _brief(tmp_path: Path, rows: list[dict[str, str]]) -> str:
    request = SimpleNamespace(
        row=SimpleNamespace(cls="class-a"), parent="parent-a", state_dir=tmp_path
    )
    path = exception_queue._brief(request, "lane-a", rows, "sonnet")
    return path.read_text(encoding="utf-8")


def test_a_class_with_455_kb_of_rows_writes_a_brief_under_the_bound(
    tmp_path: Path,
) -> None:
    rows = _rows(150, 3000)
    assert sum(len(r["evidence"]) for r in rows) > 450_000
    text = _brief(tmp_path, rows)
    assert len(text.encode("utf-8")) <= exception_queue.BRIEF_MAX_BYTES
    # The newest row is always pasted, and the brief says how many it left out and where they are.
    assert "id: i149" in text
    assert "rows left out" in text
    assert exception_queue.QUEUE_FILE in text
    assert "Opens PR in: repo-a" in text


def test_a_small_class_is_still_pasted_verbatim(tmp_path: Path) -> None:
    rows = _rows(2, 40)
    text = _brief(tmp_path, rows)
    for row in rows:
        assert f"evidence: {row['evidence']}" in text
    assert "rows left out" not in text
