"""Every lab-work run check in contracts/OMN-26115.yaml names a bus lane."""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT = Path(__file__).resolve().parents[2] / "contracts" / "OMN-26115.yaml"


def _lab_work_values(contract: dict) -> list[str]:
    return [
        check["check_value"]
        for item in contract["dod_evidence"]
        for check in item.get("checks", [])
        if check.get("check_type") == "command"
        and check["check_value"].startswith("env ")
        and " onex lab-work run " in check["check_value"]
    ]


def test_every_lab_work_check_names_a_bus_lane() -> None:
    values = _lab_work_values(yaml.safe_load(CONTRACT.read_text()))
    assert values, "contract holds no lab-work check"
    for value in values:
        assert "--bus-lane " in value, value


def test_a_lab_work_check_without_a_bus_lane_is_refused() -> None:
    planted = {
        "dod_evidence": [
            {
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": "env X=1 onex lab-work run --repo r",
                    }
                ]
            }
        ]
    }
    values = _lab_work_values(planted)
    assert values
    assert not all("--bus-lane " in v for v in values)
