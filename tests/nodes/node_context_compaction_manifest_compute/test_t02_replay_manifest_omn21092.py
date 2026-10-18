# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plan task T0.2 pinned replay manifest over captured lab events (OMN-21092)."""

from __future__ import annotations

import re
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
import yaml


def _load_manifest() -> dict[str, Any]:
    path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "context_compaction"
        / "manifest.yaml"
    )
    manifest: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return manifest


MANIFEST: dict[str, Any] = _load_manifest()
SHA256_FIELDS: frozenset[str] = frozenset(
    {"sha256", "prefix_sha256", "prompt_sha256", "content_sha256"}
)


def _assert_sha256_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in SHA256_FIELDS:
                assert isinstance(child, str)
                assert re.fullmatch(r"[0-9a-f]{64}", child), key
            _assert_sha256_fields(child)
    elif isinstance(value, list):
        for child in value:
            _assert_sha256_fields(child)


@pytest.mark.unit
def test_t02_manifest_schema_and_task() -> None:
    assert MANIFEST["schema"] == "omnimarket/context_compaction_manifest/1"
    assert MANIFEST["plan_task"] == "T0.2"


@pytest.mark.unit
def test_t02_manifest_has_three_confirmed_cases() -> None:
    cases: list[dict[str, Any]] = MANIFEST["cases"]
    assert sum(case["census_status"] == "CONFIRMED" for case in cases) >= 3
    case_ids = [case["case_id"] for case in cases]
    assert len(case_ids) == len(set(case_ids))


@pytest.mark.unit
def test_t02_manifest_two_boundary_unrecorded_case() -> None:
    assert any(
        case["two_boundaries"] is True
        and len(case["boundaries"]) >= 2
        and case["unrecorded_instruction_before_first_boundary"] is True
        and any(
            statement["ledger_stamp"] is None
            and statement["not_recorded_at_first_boundary"] is True
            for statement in case["scoring"]["expected_statements"]
        )
        for case in MANIFEST["cases"]
    )


@pytest.mark.unit
def test_t02_manifest_pins_are_sha256() -> None:
    _assert_sha256_fields(MANIFEST)
    for case in MANIFEST["cases"]:
        chain = case["transcript_chain"]
        for field in ("bytes", "records"):
            assert type(chain[field]) is int
            assert chain[field] > 0
        assert chain["bundle_path"].startswith(case["case_id"] + "/")
        for ledger_file in case["ledger"]["files"]:
            assert type(ledger_file["prefix_bytes"]) is int
            assert ledger_file["prefix_bytes"] >= 0
            assert (
                ledger_file["bundle_path"]
                == "ledger/" + ledger_file["prefix_sha256"] + ".prefix"
            )


@pytest.mark.unit
def test_t02_manifest_scoring_written() -> None:
    for case in MANIFEST["cases"]:
        scoring = case["scoring"]
        for field in ("question", "expected_answer"):
            assert isinstance(scoring[field], str)
            assert scoring[field].strip()
        assert scoring["expected_statements"]
        assert scoring["post_boundary_evidence"]
        for statement in scoring["expected_statements"]:
            assert statement["quote"] or statement["required_spans"]
            stamp = statement["ledger_stamp"]
            if stamp is not None:
                assert isinstance(stamp, str)
                assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)
        for evidence in scoring["post_boundary_evidence"]:
            assert evidence["kind"] in {"RESTATED", "REASKED", "ACTED_AGAINST"}


@pytest.mark.unit
def test_t02_manifest_exclusions_named() -> None:
    for case in MANIFEST["cases"]:
        assert isinstance(case["model_id"], str)
        assert case["model_id"].strip()
        for field, factor in (
            ("checkpoint", "ORCHESTRATION_STATE"),
            ("branch", "REPO_STATE"),
        ):
            exclusion = case[field]
            if exclusion["status"] == "EXCLUDED":
                assert exclusion["factor"] == factor
                assert isinstance(exclusion["reason"], str)
                assert exclusion["reason"].strip()


@pytest.mark.unit
def test_t02_manifest_boundaries_ordered() -> None:
    for case in MANIFEST["cases"]:
        times = [
            datetime.fromisoformat(boundary["pre_at"])
            for boundary in case["boundaries"]
        ]
        assert all(earlier < later for earlier, later in pairwise(times))
        assert all(boundary["paired"] is True for boundary in case["boundaries"])
