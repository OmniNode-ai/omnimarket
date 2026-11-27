# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Integration-proof dod commands name the lab bus lane they publish on.

``onex lab-work run`` without ``--bus-lane`` publishes nowhere the lab serve
process listens, so the closer refuses the ticket. Each contract below binds an
integration-proof item whose command must carry ``--bus-lane dev``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
_TICKETS = (
    "OMN-26534",
    "OMN-25827",
    "OMN-25912",
    "OMN-26121",
    "OMN-26678",
    "OMN-26219",
    "OMN-26520",
)


def _lab_work_commands(ticket: str) -> list[str]:
    doc = yaml.safe_load((_CONTRACTS / f"{ticket}.yaml").read_text())
    out: list[str] = []
    for item in doc.get("dod_evidence", []):
        for check in item.get("checks", []):
            value = str(check.get("check_value", ""))
            if "lab-work run" in value:
                out.append(" ".join(value.split()))
    return out


@pytest.mark.parametrize("ticket", _TICKETS)
def test_lab_work_run_names_the_dev_bus_lane(ticket: str) -> None:
    commands = _lab_work_commands(ticket)
    assert commands, f"{ticket} binds no lab-work run command"
    for command in commands:
        assert re.search(r"lab-work run --bus-lane dev ", command), command
