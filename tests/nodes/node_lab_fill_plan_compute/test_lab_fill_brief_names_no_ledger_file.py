# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A rendered lane brief points a lane at the ruling through the ledger reader, never at a ledger file path."""

from __future__ import annotations

from typing import Any

import pytest

from .test_lab_fill_render_replays_lab_fill_js import _CASES, _IDS, _render

pytestmark = pytest.mark.unit

_LEDGER_FILE = "ROLLING_WORK" + "_LEDGER"


def test_the_recorded_cases_are_there() -> None:
    assert len(_CASES) > 0


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_the_authority_line_names_the_ruling_lookup_and_no_ledger_file(
    case: dict[str, Any],
) -> None:
    brief = _render(case)
    authority = next(
        line for line in brief.splitlines() if line.startswith("AUTHORITY:")
    )
    assert _LEDGER_FILE not in brief
    assert "in the rolling work ledger (find it with lane_brief.py grep '" in authority
