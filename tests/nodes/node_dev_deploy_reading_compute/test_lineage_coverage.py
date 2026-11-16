# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lineage coverage (OMN-23603): a PASS receipt for a descendant covers an earlier merge, and a
running sha that descends from the expected one passes, both only when the request's rule allows it
and the clone read the pair; an unread pair is never a pass."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReading,
    ModelLineageFact,
    ModelLineageRule,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from tests.nodes.node_dev_deploy_reading_compute.dev_deploy_support import (
    LANE,
    SHA_A,
    SHA_B,
    SHA_C,
    lineage_fact,
    merge,
    probe,
    receipt,
    request,
    resolve_handler,
    rule,
)

SHA_D = "d" * 40


def _read(**edits: Any) -> ModelDevDeployReading:
    return resolve_handler()().handle(request(**edits))


def _reasons(**edits: Any) -> tuple[str, ...]:
    return _read(**edits).reasons


def _receipt_rule(**edits: Any) -> dict[str, Any]:
    return {**rule(receipt_covers_descendant=True), **edits}


def _probe_rule(**edits: Any) -> dict[str, Any]:
    return {**rule(probe_accepts_descendant=True), **edits}


def test_lineage_receipt_descendant_pass_receipt_covers_the_earlier_merge() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=_receipt_rule(),
        lineage=[lineage_fact(SHA_A, SHA_B)],
    )
    assert reading.verdict is V.WORKING
    assert reading.merges_receipted == 1


def test_lineage_receipt_not_ancestor_does_not_cover() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=_receipt_rule(),
        lineage=[lineage_fact(SHA_A, SHA_B, relation="not-ancestor")],
    )
    assert reading.verdict is V.NOT_WORKING
    assert any(r.startswith("no-pass-receipt:") for r in reading.reasons)


def test_lineage_receipt_unread_pair_is_unknown_never_working() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=_receipt_rule(),
        lineage=[
            lineage_fact(SHA_A, SHA_B, relation="unread", unread_reason="clone-api-503")
        ],
    )
    assert reading.verdict is V.UNKNOWN
    assert any(r.startswith("receipt-lineage-unread:") for r in reading.reasons)


def test_lineage_receipt_absent_fact_is_unknown() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=_receipt_rule(),
        lineage=[],
    )
    assert reading.verdict is V.UNKNOWN
    assert any(r.startswith("receipt-lineage-unread:") for r in reading.reasons)


def test_lineage_receipt_too_many_commits_between_is_not_working() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=_receipt_rule(max_commits_between=1),
        lineage=[lineage_fact(SHA_A, SHA_B, commits_between=2)],
    )
    assert reading.verdict is V.NOT_WORKING


def test_lineage_receipt_finished_after_the_cover_window_is_not_working() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T09:00:01Z")],
        lineage_rule=_receipt_rule(receipt_cover_window_seconds=3600),
        lineage=[lineage_fact(SHA_A, SHA_B)],
    )
    assert reading.verdict is V.NOT_WORKING


def test_lineage_receipt_fail_receipt_does_not_cover() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z", result="FAIL")],
        lineage_rule=_receipt_rule(),
        lineage=[lineage_fact(SHA_A, SHA_B)],
    )
    assert reading.verdict is V.NOT_WORKING


def test_lineage_receipt_other_lane_does_not_cover() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z", lane="lane-b")],
        lineage_rule=_receipt_rule(),
        lineage=[lineage_fact(SHA_A, SHA_B)],
    )
    assert reading.verdict is V.NOT_WORKING
    assert LANE != "lane-b"


def test_lineage_receipt_rule_switched_off_is_not_working() -> None:
    reading = _read(
        merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
        receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
        lineage_rule=rule(receipt_covers_descendant=False),
        lineage=[lineage_fact(SHA_A, SHA_B)],
    )
    assert reading.verdict is V.NOT_WORKING


def test_lineage_probe_descendant_with_no_runtime_commits_is_working() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=_probe_rule(),
        lineage=[lineage_fact(SHA_B, SHA_C)],
    )
    assert reading.verdict is V.WORKING


def test_lineage_probe_runtime_commit_between_is_not_working() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=_probe_rule(),
        lineage=[lineage_fact(SHA_B, SHA_C, runtime_affecting_between=(SHA_A,))],
    )
    assert reading.verdict is V.NOT_WORKING
    assert any(r.startswith("probe-runtime-commit-between:") for r in reading.reasons)


def test_lineage_probe_not_ancestor_is_not_working() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=_probe_rule(),
        lineage=[lineage_fact(SHA_B, SHA_C, relation="not-ancestor")],
    )
    assert reading.verdict is V.NOT_WORKING
    assert any(r.startswith("probe-sha-mismatch:") for r in reading.reasons)


def test_lineage_probe_unread_pair_is_unknown() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=_probe_rule(),
        lineage=[
            lineage_fact(SHA_B, SHA_C, relation="unread", unread_reason="clone-api-503")
        ],
    )
    assert reading.verdict is V.UNKNOWN
    assert any(r.startswith("probe-lineage-unread:") for r in reading.reasons)


def test_lineage_probe_too_many_commits_between_is_not_working() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=_probe_rule(max_commits_between=1),
        lineage=[lineage_fact(SHA_B, SHA_C, commits_between=2)],
    )
    assert reading.verdict is V.NOT_WORKING
    assert any(r.startswith("probe-descendant-too-far:") for r in reading.reasons)


def test_lineage_probe_rule_switched_off_is_not_working() -> None:
    reading = _read(
        probes=[probe(expected=SHA_B, observed=SHA_C)],
        lineage_rule=rule(probe_accepts_descendant=False),
        lineage=[lineage_fact(SHA_B, SHA_C)],
    )
    assert reading.verdict is V.NOT_WORKING
    assert any(r.startswith("probe-sha-mismatch:") for r in reading.reasons)


def test_lineage_model_refuses_a_non_sha() -> None:
    with pytest.raises(ValidationError, match="not an exact sha"):
        ModelLineageFact(
            base_sha="abc", head_sha=SHA_B, relation="ancestor", commits_between=1
        )


def test_lineage_model_refuses_an_ancestor_with_no_commit_count() -> None:
    with pytest.raises(ValidationError, match="commit count"):
        ModelLineageFact(base_sha=SHA_A, head_sha=SHA_B, relation="ancestor")


def test_lineage_model_refuses_an_unread_pair_with_no_reason() -> None:
    with pytest.raises(ValidationError, match="reason"):
        ModelLineageFact(
            base_sha=SHA_A, head_sha=SHA_B, relation="unread", unread_reason=None
        )


def test_lineage_model_refuses_a_zero_commit_bound() -> None:
    with pytest.raises(ValidationError):
        ModelLineageRule(
            receipt_covers_descendant=True,
            probe_accepts_descendant=True,
            max_commits_between=0,
            receipt_cover_window_seconds=86400,
        )
