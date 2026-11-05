# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host roles preserve legacy tables and enforce explicit publishing ownership."""

import pytest
from pydantic import ValidationError

from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_desired_publisher_refuses_wrong_count(count: int) -> None:
    rows = [
        {"name": "host-a", "target": "host-a", "roles": ["lab"]},
        *[
            {
                "name": f"publisher-{index}",
                "target": f"publisher-{index}",
                "roles": ["desired-publisher"],
            }
            for index in range(count)
        ],
    ]
    with pytest.raises(ValidationError, match=rf"desired-publisher.*{count}"):
        ModelLabHostsTable.model_validate({"hosts": rows})


def test_exactly_one_desired_publisher_accepts_one() -> None:
    table = ModelLabHostsTable.model_validate(
        {
            "hosts": [
                {
                    "name": "host-a",
                    "target": "host-a",
                    "roles": ["lab", "desired-publisher"],
                },
                {"name": "host-b", "target": "host-b", "roles": ["lab-proof"]},
                {"name": "host-c", "target": "host-c"},
            ]
        }
    )
    assert table.hosts[0].roles == ("lab", "desired-publisher")


@pytest.mark.parametrize("roles", [{}, {"roles": None}, {"roles": []}])
def test_rows_without_roles_load(roles: dict[str, object]) -> None:
    table = ModelLabHostsTable.model_validate(
        {"hosts": [{"name": "host-a", "target": "host-a", **roles}]}
    )
    assert table.hosts[0].name == "host-a"
    assert not table.hosts[0].roles


def test_launcher_requires_cap_zero_and_no_lane_role_accepts_zero() -> None:
    row = ModelLabHostRow(name="host-a", target="host-a", cap=0, roles=("launcher",))
    assert row.roles == ("launcher",)


@pytest.mark.parametrize("cap", [1, None])
def test_launcher_requires_cap_zero_and_no_lane_role_refuses_cap(
    cap: int | None,
) -> None:
    with pytest.raises(ValidationError, match=r"launcher.*cap"):
        ModelLabHostRow(name="host-a", target="host-a", cap=cap, roles=("launcher",))


@pytest.mark.parametrize("role", ["lab", "lab-proof"])
def test_launcher_requires_cap_zero_and_no_lane_role_refuses_lane(role: str) -> None:
    with pytest.raises(ValidationError, match="launcher"):
        ModelLabHostRow.model_validate(
            {
                "name": "host-a",
                "target": "host-a",
                "cap": 0,
                "roles": ["launcher", role],
            }
        )


@pytest.mark.parametrize("role", ["lab-proof", "lab", "launcher", "desired-publisher"])
def test_unavailable_host_has_no_role(role: str) -> None:
    with pytest.raises(ValidationError, match="unavailable"):
        ModelLabHostRow.model_validate(
            {
                "name": "host-a",
                "target": "host-a",
                "available": False,
                "cap": 0,
                "roles": [role],
            }
        )
    for roles in (None, ()):
        assert not ModelLabHostRow(
            name="host-a", target="host-a", available=False, roles=roles
        ).roles


def test_host_roles_must_not_repeat() -> None:
    with pytest.raises(ValidationError, match="repeat"):
        ModelLabHostRow(name="host-a", target="host-a", roles=("lab", "lab"))


def test_host_roles_refuse_unknown_role() -> None:
    with pytest.raises(ValidationError, match="roles"):
        ModelLabHostRow.model_validate(
            {"name": "host-a", "target": "host-a", "roles": ["unknown"]}
        )
