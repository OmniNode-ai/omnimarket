# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22675: the AC3 read command bounds the latency, and the oracle comparison ignores tie order."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.test_omn22675_savings_unpriced_single_pass_real_postgres import _comparable

_CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "OMN-22675.yaml"
_BOUND_S = 5


def _integration_proof_command() -> str:
    contract = yaml.safe_load(_CONTRACT.read_text("utf-8"))
    (item,) = (
        d
        for d in contract["dod_evidence"]
        if d["id"] == "dod-omn22675-integration-proof"
    )
    return str(item["checks"][0]["check_value"])


def _verdict(reply: str) -> int:
    """Run the check's decision stage on a curl reply of ``<http code> <seconds>``."""
    command = _integration_proof_command()
    found = re.search(r"\| (awk '[^']*')\)\" = (\S+)$", command)
    assert found, "the command decides on more than the http code"
    awk, expected = found.groups()
    said = subprocess.run(
        ["sh", "-c", awk], input=reply, text=True, check=False, capture_output=True
    ).stdout.strip()
    return 0 if said == expected else 1


@pytest.mark.parametrize("reply", ["200 0.8\n", "200 4.99\n"])
def test_golden_a_fast_answer_passes_the_ac3_command(reply: str) -> None:
    assert _verdict(reply) == 0


@pytest.mark.parametrize("reply", ["200 8.0\n", "200 5.0\n", "503 0.2\n"])
def test_error_a_slow_or_failed_answer_fails_the_ac3_command(reply: str) -> None:
    assert _verdict(reply) != 0


def test_the_ac3_command_measures_the_time_the_read_took() -> None:
    command = _integration_proof_command()
    assert "%{time_total}" in command
    assert f"< {_BOUND_S}" in command


def _row(sessions: Any) -> dict[str, Any]:
    return {"tenant_id": "t", "session_count": 2, "sessions": sessions}


def test_golden_tied_sessions_in_another_order_compare_equal() -> None:
    a = [{"session_id": "s-1", "cost": 1}, {"session_id": "s-2", "cost": 2}]
    assert _comparable(_row(a)) == _comparable(_row(list(reversed(a))))


def test_golden_a_json_text_array_compares_with_the_decoded_one() -> None:
    a = [{"session_id": "s-1"}, {"session_id": "s-2"}]
    text = '[{"session_id": "s-2"}, {"session_id": "s-1"}]'
    assert _comparable(_row(text)) == _comparable(_row(a))


def test_error_a_changed_session_value_still_compares_unequal() -> None:
    a = [{"session_id": "s-1", "cost": 1}, {"session_id": "s-2", "cost": 2}]
    b = [{"session_id": "s-1", "cost": 1}, {"session_id": "s-2", "cost": 3}]
    assert _comparable(_row(a)) != _comparable(_row(b))


def test_error_a_missing_session_still_compares_unequal() -> None:
    a = [{"session_id": "s-1"}, {"session_id": "s-2"}]
    assert _comparable(_row(a)) != _comparable(_row(a[:1]))
