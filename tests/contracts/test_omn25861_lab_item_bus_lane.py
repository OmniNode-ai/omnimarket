"""The OMN-25861 integration-proof item names a bus lane.

``onex lab-work run`` resolves to the shared kafka bus and refuses with "names no
broker" unless it is given ``--bus-lane``. The item must carry it before ``--``.
"""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-25861.yaml"
_ITEM = "dod-omn25861-integration-proof"


def test_the_lab_work_run_names_a_bus_lane_before_the_lab_command() -> None:
    document = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    entries = [e for e in document["dod_evidence"] if e["id"] == _ITEM]
    assert len(entries) == 1
    words = shlex.split(entries[0]["checks"][0]["check_value"])
    caller = words[: words.index("--")]
    assert "--bus-lane" in caller, caller
    assert caller[caller.index("--bus-lane") + 1] == "dev"
