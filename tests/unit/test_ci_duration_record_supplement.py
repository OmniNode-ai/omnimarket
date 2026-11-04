# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The duration supplement retains measurements still absent from dev."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
RECORD = Path(__file__).resolve().parents[2] / "config" / "test_file_durations.json"


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/nodes/node_exception_queue_effect/test_andon_ledger_holds.py",
        "tests/test_golden_chain_projection_write_effect.py",
        "tests/test_omn18883_projection_write_effect_real_postgres.py",
        "tests/test_retired_repository_reconcile.py",
        "tests/unit/nodes/node_work_ledger_append_effect/test_append_if_absent.py",
    ],
)
def test_supplement_retains_the_remaining_measured_files(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"measured test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert seconds > 0
