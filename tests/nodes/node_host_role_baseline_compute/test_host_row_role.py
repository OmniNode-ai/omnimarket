# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab host table row carries an optional host role from the four roles (OMN-24263)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.models.model_host_role import EnumHostRole
from omnimarket.models.overlay_keys.model_overlay_documents import (
    ModelLabHostRow,
    ModelLabHostsTable,
)

pytestmark = pytest.mark.unit


def test_a_row_accepts_each_of_the_four_roles() -> None:
    for role in ("lab-worker", "gpu-model", "runtime-host", "operator-mac"):
        row = ModelLabHostRow.model_validate(
            {"name": "h", "target": "h", "host_role": role}
        )
        assert row.host_role == EnumHostRole(role)


def test_a_row_refuses_an_unknown_role() -> None:
    with pytest.raises(ValidationError, match="host_role"):
        ModelLabHostRow.model_validate(
            {"name": "h", "target": "h", "host_role": "gpu_box"}
        )


def test_a_table_with_no_declared_role_loads_unchanged() -> None:
    table = ModelLabHostsTable.model_validate({"hosts": [{"name": "h", "target": "h"}]})
    assert table.hosts[0].host_role is None


def test_the_overlay_stores_roles_key_is_not_the_host_role() -> None:
    row = ModelLabHostRow.model_validate(
        {"name": "h", "target": "h", "roles": ["lab"], "host_role": "lab-worker"}
    )
    assert row.roles == ("lab",)
    assert row.host_role == EnumHostRole.LAB_WORKER
