# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local environment cursor's ownership, declared ahead of its vendoring (OMN-19988).

omnibase_infra vendors node_projection_local_environment's migrations before
this repo ships them, and its application SQL gate needs the sequence the
writer grant names to have exactly one owner in this manifest. Failure modes:
the sequence is not declared, is declared twice, or is declared under another
domain or owner than the tenant projection's migration runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts/application-relation-ownership.yaml"
)


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def test_local_environment_cursor_sequence_is_declared_exactly_once() -> None:
    objects = list(_manifest()["database_objects"])
    sequence_name = "local_environment_projection_cursor_seq"
    matches = [row for row in objects if row["name"] == sequence_name]
    assert len(matches) == 1, (
        f"expected exactly one {sequence_name} object, got {matches}"
    )
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == "public"
    assert row["domain"] == "TENANT"
    assert row["owner_declaration"] == (
        "service:omnimarket_projection_migration_runner"
    )
    assert row["writers"] == []
