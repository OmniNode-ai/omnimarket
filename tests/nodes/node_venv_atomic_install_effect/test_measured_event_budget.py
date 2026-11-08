# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The recorded strace reading of one real pass over a copy of the dispatch venv."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).with_name("measurement_omn22712.json")


def test_one_pass_over_a_37k_file_venv_makes_at_most_40000_events() -> None:
    reading = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert reading["venv_copy"]["files"] >= 37000
    assert reading["new_pass"]["status"] == "installed"
    assert reading["new_pass"]["total_syscalls"] <= reading["limit"] == 40000
    assert (
        reading["new_pass"]["total_syscalls"] < reading["recorded_before_file_events"]
    )
