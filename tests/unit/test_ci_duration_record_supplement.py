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


@pytest.mark.parametrize(
    "test_file",
    [
        "tests/integration/test_join_j16.py",
        "tests/nodes/node_factory_floor_publish_effect/test_compare_page.py",
        "tests/nodes/node_factory_floor_publish_effect/test_join_j2_step.py",
        "tests/nodes/node_factory_floor_publish_effect/test_join_refusal_step.py",
        "tests/nodes/node_factory_floor_publish_effect/test_simple_page_piece_measure.py",
        "tests/nodes/node_floor_compare_effect/test_piece_measure.py",
        "tests/nodes/node_floor_emission_compute/test_refusal_is_not_an_upstream_failure.py",
        "tests/unit/test_omn22867_shared_red_causes.py",
        "tests/validation/test_j8_join_is_not_auto_wirable_omn23076.py",
    ],
)
def test_omn23054_supplement_records_each_measured_file(test_file: str) -> None:
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    assert test_file in record, f"measured test file missing from record: {test_file}"
    seconds = record[test_file]
    assert isinstance(seconds, (int, float))
    assert not isinstance(seconds, bool)
    assert math.isfinite(seconds)
    assert seconds >= 0


def test_omn23054_supplement_has_provenance_entry() -> None:
    provenance = json.loads(
        (RECORD.parent / "test_file_durations_provenance.json").read_text(
            encoding="utf-8"
        )
    )
    assert provenance["supplement_omn23054"]["files_added"] == 9
