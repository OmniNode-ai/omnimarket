"""Every lab-work run check in the closer's contracts names a bus lane and a servable head.

The local Done gate runs each integration-proof check as written. A check without
--bus-lane refuses before it publishes; a check whose inner command starts with env
is refused by the serving host, which runs only a declared set of executables.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
TICKETS = (
    "OMN-25827",
    "OMN-25912",
    "OMN-26121",
    "OMN-26219",
    "OMN-26316",
    "OMN-26520",
    "OMN-26534",
    "OMN-26678",
    "OMN-26726",
)


def _lab_work_values(contract: dict) -> list[str]:
    values: list[str] = []
    for item in contract.get("dod_evidence", []):
        for check in item.get("checks", []):
            value = " ".join(str(check.get("check_value", "")).split())
            if (
                check.get("check_type") == "command"
                and " onex lab-work run " in f" {value} "
            ):
                values.append(value)
    return values


def _problems(value: str) -> list[str]:
    found: list[str] = []
    if "--bus-lane " not in value:
        found.append("no --bus-lane")
    inner = value.split(" -- ", 1)[1] if " -- " in value else ""
    if inner.startswith("env "):
        found.append("inner command starts with env")
    return found


@pytest.mark.parametrize("ticket", TICKETS)
def test_every_lab_work_check_is_servable(ticket: str) -> None:
    contract = yaml.safe_load((CONTRACTS / f"{ticket}.yaml").read_text())
    values = _lab_work_values(contract)
    assert values, f"{ticket} holds no lab-work check"
    for value in values:
        assert _problems(value) == [], value


def test_a_check_without_a_bus_lane_is_refused() -> None:
    planted = "onex lab-work run --repo r --kind test -- uv run pytest x"
    assert _problems(planted) == ["no --bus-lane"]


def test_a_check_with_an_env_inner_command_is_refused() -> None:
    planted = "onex lab-work run --bus-lane dev --repo r --kind test -- env X=1 uv run pytest x"
    assert _problems(planted) == ["inner command starts with env"]


def test_a_servable_check_passes() -> None:
    planted = "onex lab-work run --bus-lane dev --repo r --kind test -- uv run pytest x"
    assert _problems(planted) == []
