# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15602 — ``probe_stdout`` on a node_dod_verify receipt must always parse.

The receipt written to ``<ONEX_EVIDENCE_ROOT>/<TICKET>/dod_report.json`` is the
only third-party-readable proof that a DoD run happened. It stored the whole
per-check evidence payload as a serialized JSON *string* in ``probe_stdout`` and
then sliced that string at 4096 chars — a slice applied to the **serialized
document** rather than to the payload, so it cut mid-token inside the
``details`` array. Every receipt with more than a handful of checks therefore
failed ``json.loads``: the head counters survived (they serialize first) but the
body — which check ran and what it said — was destroyed, leaving an
unfalsifiable head count with no inspectable body.

These tests drive the real ``_build_receipt`` (and the real CLI write path) on
synthetic states large enough to exceed the cap, and assert the receipt always
round-trips through ``json.loads`` and names its own elision.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_dod_verify.__main__ import (
    _PROBE_STDOUT_MAX_CHARS,
    _build_receipt,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)

pytestmark = pytest.mark.unit


def _state(
    *,
    n_checks: int,
    message_chars: int = 300,
    ticket_id: str = "OMN-15602",
) -> ModelDodVerifyState:
    """Build a VERIFIED state with ``n_checks`` checks of a realistic size."""
    checks = [
        ModelEvidenceCheckResult(
            evidence_id=f"dod-{i:03d}",
            description=f"evidence item {i} — verified by command probe",
            status=EnumEvidenceCheckStatus.VERIFIED,
            message="x" * message_chars,
        )
        for i in range(n_checks)
    ]
    return ModelDodVerifyState(
        correlation_id=uuid4(),
        ticket_id=ticket_id,
        status=EnumDodVerifyStatus.VERIFIED,
        checks=checks,
        total_checks=n_checks,
        verified_count=n_checks,
    )


def _payload(receipt: dict[str, object]) -> dict[str, Any]:
    """Parse ``probe_stdout`` off a receipt dict, failing loudly if it cannot."""
    probe_stdout = receipt["probe_stdout"]
    assert isinstance(probe_stdout, str)
    parsed: Any = json.loads(probe_stdout)
    assert isinstance(parsed, dict)
    return parsed


@pytest.mark.parametrize("n_checks", [1, 7, 12, 18, 25, 200])
def test_probe_stdout_always_parses(n_checks: int, tmp_path: Path) -> None:
    """AC1: ``json.loads(receipt["probe_stdout"])`` succeeds at every size.

    RED before OMN-15602: the 25-check case reproduced the live failure on
    ``evidence/OMN-15488/dod_report.json`` (``Unterminated string starting at:
    line 1 column 3956``); 200 checks fails the same way.
    """
    receipt = _build_receipt(_state(n_checks=n_checks), None, tmp_path)
    payload = _payload(receipt)

    assert payload["total"] == n_checks
    assert payload["verified"] == n_checks
    assert isinstance(payload["details"], list)


def test_complete_payload_carries_every_detail_and_reports_no_elision(
    tmp_path: Path,
) -> None:
    """A 25-check run fits the cap outright: all details present, elided == 0.

    25 checks is the size of the live ``OMN-15488`` receipt named in the
    ticket — the canonical "should have been readable" case.
    """
    receipt = _build_receipt(_state(n_checks=25), None, tmp_path)
    payload = _payload(receipt)

    assert payload["details_total"] == 25
    assert payload["details_elided"] == 0
    assert len(payload["details"]) == 25
    assert [d["id"] for d in payload["details"]] == [f"dod-{i:03d}" for i in range(25)]


def test_elision_is_explicit_and_payload_still_parses(tmp_path: Path) -> None:
    """AC2: past the cap, the payload names how many details were dropped.

    A consumer can tell "elided" from "complete" without guessing: the
    surviving details are a prefix of the run and ``details_elided`` is the
    exact count that did not fit.
    """
    n_checks = 4_000
    receipt = _build_receipt(_state(n_checks=n_checks), None, tmp_path)
    payload = _payload(receipt)

    probe_stdout = receipt["probe_stdout"]
    assert isinstance(probe_stdout, str)
    assert len(probe_stdout) <= _PROBE_STDOUT_MAX_CHARS

    assert payload["details_total"] == n_checks
    assert payload["details_elided"] > 0
    assert len(payload["details"]) + payload["details_elided"] == n_checks
    # The verdict counters are never elided — the head must survive intact.
    assert payload["total"] == n_checks
    assert payload["verified"] == n_checks


def test_single_oversized_message_is_truncated_in_the_payload(tmp_path: Path) -> None:
    """One pathological message cannot produce invalid JSON.

    A message larger than the whole cap is truncated at the *payload* level
    (before serialization) and self-describes the truncation, so the document
    still parses instead of being cut mid-token.
    """
    receipt = _build_receipt(
        _state(n_checks=1, message_chars=_PROBE_STDOUT_MAX_CHARS * 3), None, tmp_path
    )
    payload = _payload(receipt)

    assert payload["details_total"] == 1
    assert len(payload["details"]) == 1
    message = payload["details"][0]["message"]
    assert isinstance(message, str)
    assert "truncated" in message


def test_probe_stdout_stays_non_empty_for_executable_check_type(
    tmp_path: Path,
) -> None:
    """Ask 3: the ModelDodReceipt rule-3 invariant is preserved.

    ``check_type="command"`` is executable, so an empty ``probe_stdout`` would
    be rejected by the model itself — elision must never bottom out at "".
    """
    for n_checks in (0, 1, 4_000):
        receipt = _build_receipt(_state(n_checks=n_checks), None, tmp_path)
        probe_stdout = receipt["probe_stdout"]
        assert isinstance(probe_stdout, str)
        assert probe_stdout.strip()


def test_written_receipt_parses_end_to_end(tmp_path: Path) -> None:
    """AC3 (write path): the receipt the CLI actually persists parses.

    Drives ``python -m omnimarket.nodes.node_dod_verify`` over a contract with
    enough passing checks to blow the old 4096-char cap, then re-reads the
    durable ``dod_report.json`` the way a third party would.
    """
    ticket_id = "OMN-15602"
    contract = {
        "schema_version": "1.0.0",
        "ticket_id": ticket_id,
        "dod_evidence": [
            {
                "id": f"dod-{i:03d}",
                "description": (
                    f"evidence item {i} with a description long enough that "
                    "thirty of them overflow a four-kilobyte serialized document"
                ),
                "checks": [{"check_type": "command", "command": "true"}],
            }
            for i in range(30)
        ],
    }
    contract_path = tmp_path / f"{ticket_id}.yaml"
    contract_path.write_text(yaml.dump(contract), encoding="utf-8")
    output_path = tmp_path / "dod_report.json"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnimarket.nodes.node_dod_verify",
            "--ticket-id",
            ticket_id,
            "--contract-path",
            str(contract_path),
            "--output-path",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert output_path.exists(), f"no receipt written: {proc.stderr}"

    receipt = json.loads(output_path.read_text(encoding="utf-8"))
    payload = json.loads(receipt["probe_stdout"])

    assert payload["details_total"] == 30
    assert payload["total"] == 30
    assert isinstance(payload["details_elided"], int)
