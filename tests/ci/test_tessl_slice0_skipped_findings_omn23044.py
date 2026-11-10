"""OMN-23044 AC2: every finding not fixed in slice 0 is listed with its reason.

The list is a repository file, so the check reads the change, not the pull request body.
It fails on any tree that lacks the file, a row, a verdict from the declared set, or a reason.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

SKIPPED_FILE = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "work-tracking"
    / "OMN-23044-slice0-skipped-findings.yaml"
)
FALSE_CRITICAL_THREAD = "PRRT_kwDOR6jjtc604qu4"


def _document() -> dict[str, Any]:
    assert SKIPPED_FILE.is_file(), f"missing skipped-findings list: {SKIPPED_FILE}"
    loaded = yaml.safe_load(SKIPPED_FILE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _rows() -> list[dict[str, str]]:
    findings = _document().get("findings")
    assert isinstance(findings, list), "the skipped-findings list is not a list"
    assert findings, "the skipped-findings list has no rows"
    return findings


@pytest.mark.unit
def test_every_row_has_thread_pr_severity_verdict_and_reason() -> None:
    verdicts = set(_document()["verdicts"])
    for row in _rows():
        assert set(row) == {
            "thread",
            "pull_request",
            "severity",
            "verdict",
            "reason",
        }, row
        assert row["thread"].startswith("PRRT_"), row
        assert "#" in row["pull_request"], row
        assert row["severity"] in {"Critical", "Major", "Minor"}, row
        assert row["verdict"] in verdicts, row
        assert row["reason"].strip(), row


@pytest.mark.unit
def test_one_row_per_thread() -> None:
    threads = [row["thread"] for row in _rows()]
    assert len(threads) == len(set(threads))


@pytest.mark.unit
def test_false_critical_is_listed_as_skipped_and_not_edited() -> None:
    by_thread = {row["thread"]: row for row in _rows()}
    assert FALSE_CRITICAL_THREAD in by_thread
    assert by_thread[FALSE_CRITICAL_THREAD]["verdict"] == "SKIPPED"
