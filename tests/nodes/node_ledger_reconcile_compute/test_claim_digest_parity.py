# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claim digests match the ledger writer's reference normalization (OMN-20677)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from omnimarket.nodes.node_ledger_reconcile_compute.handlers import pairing

pytestmark = pytest.mark.unit

STAMP = "2026-09-20T10:05:00Z"
RETRY_STAMP = "2026-09-20T10:06:00Z"
BODY = "| CLAIM | lane=token-lane | ticket=OMN-101 | fix parser"


@pytest.mark.parametrize(
    "row",
    [
        pytest.param("", id="empty"),
        pytest.param(" \n\t\n", id="blank-lines"),
        pytest.param(BODY, id="unstamped"),
        pytest.param(f"{STAMP} {BODY}", id="timestamp-led"),
        pytest.param(f"{RETRY_STAMP} {BODY}", id="retry-stamp"),
        pytest.param(f"  - {STAMP} {BODY}  ", id="dash-bullet"),
        pytest.param(f"* {STAMP} {BODY}", id="star-bullet"),
        pytest.param(f"**{STAMP}** {BODY}", id="bold-stamp"),
        pytest.param(f"| {STAMP} {BODY} |", id="table-cell-stamp"),
        pytest.param(
            f"\n  - {STAMP} {BODY}  \n\n* **{RETRY_STAMP}** continuation\n",
            id="multiline",
        ),
        pytest.param(f"{STAMP}\n* **{STAMP}**\n{BODY}", id="stamp-only-lines"),
        pytest.param(f"{STAMP} {BODY} — repair §0a", id="unicode"),
        pytest.param(f"{STAMP} {BODY} | cites {RETRY_STAMP}", id="mid-body-stamp"),
    ],
)
def test_claim_row_digest_matches_reference_algorithm(row: str) -> None:
    # Keep the writer's reference algorithm here, independent of pairing's helpers.
    normalized_lines = []
    for line in row.splitlines():
        normalized = line.strip()
        normalized = re.sub(r"^[-*]\s+", "", normalized, count=1)
        normalized = re.sub(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*",
            "",
            normalized,
            count=1,
        )
        normalized = re.sub(
            r"^\*\*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\*\*\s*",
            "",
            normalized,
            count=1,
        )
        normalized = re.sub(
            r"^\|\s*\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s*(?=\|)",
            "|",
            normalized,
            count=1,
        )
        normalized = normalized.strip()
        if normalized:
            normalized_lines.append(normalized)
    expected = hashlib.sha256("\n".join(normalized_lines).encode("utf-8")).hexdigest()[
        :12
    ]

    digest = pairing.claim_row_digest(row)
    assert digest == expected
    assert re.fullmatch(r"[0-9a-f]{12}", digest) is not None


@pytest.mark.parametrize(
    "template",
    ["{stamp} " + BODY, "- **{stamp}** " + BODY, "| {stamp} " + BODY + " |"],
    ids=["timestamp-led", "bullet-bold", "table-cell"],
)
def test_claim_row_digest_ignores_a_changed_leading_stamp(template: str) -> None:
    original = template.format(stamp=STAMP)
    retry = template.format(stamp=RETRY_STAMP)
    assert original != retry
    assert pairing.claim_row_digest(original) == pairing.claim_row_digest(retry)


def test_claim_row_digest_preserves_a_mid_body_stamp() -> None:
    original = f"{STAMP} {BODY} | cites {STAMP}"
    changed_citation = f"{STAMP} {BODY} | cites {RETRY_STAMP}"
    assert pairing.claim_row_digest(original) != pairing.claim_row_digest(
        changed_citation
    )


CAPTURED = json.loads(
    (Path(__file__).parent / "fixtures/claim_row_digests.json").read_text()
)


@pytest.mark.parametrize(
    "case", CAPTURED["cases"], ids=[c["id"] for c in CAPTURED["cases"]]
)
def test_claim_row_digest_matches_the_ledger_writer_s_own_digest(
    case: dict[str, str],
) -> None:
    """The expected digests were produced by running the ledger writer's own function."""
    assert pairing.claim_row_digest(case["row"]) == case["digest"]
