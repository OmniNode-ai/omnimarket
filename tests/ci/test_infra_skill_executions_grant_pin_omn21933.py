# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: omnimarket pins the omnibase_infra commit that grants public.skill_executions.

node_projection_skill_lifecycle (OMN-21933) declares a write binding on
public.skill_executions for tenant_projection_writer. The runtime auto-wiring
check refuses a binding whose principal lacks the declared privileges in the
pinned omnibase_infra topology. omnibase_infra#5565 (d4aa14ddcca1) declares that
grant; the lock must pin that commit or a later infra dev tag (858c2c689461 is
a descendant of it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ci.test_sibling_core_pin_pair_omn17427 import (
    _locked_dev_tag,
    _pyproject_tag,
)

pytestmark = pytest.mark.unit

GRANT_TAG_STAMP = "20261102114311"


def _stamp(tag: str) -> str:
    return tag.split("-")[1]


def test_the_lock_and_pyproject_pin_an_infra_at_or_after_the_skill_executions_grant() -> (
    None
):
    locked_tag, _ = _locked_dev_tag("omnibase-infra")
    assert _stamp(locked_tag) >= GRANT_TAG_STAMP
    assert _pyproject_tag("omnibase-infra") == locked_tag


def test_the_previous_infra_pin_is_not_the_one_locked() -> None:
    """Negative control: 3f93a2cc6f6c predates the grant and must not be the lock."""
    assert _locked_dev_tag("omnibase-infra")[1][:12] != "3f93a2cc6f6c"


_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle/migrations"
    / "0002_grant_tenant_projection_writer_skill_executions.sql"
)


def test_the_node_migration_grants_the_writer_and_the_dashboard_reader_on_skill_executions() -> (
    None
):
    """The node carries the grant migration itself, beside the infra pin.

    The pin above is already on dev, so it cannot show what this change adds.
    The migration the node ships is what grants the writer its privileges on
    public.skill_executions and the RLS-scoped dashboard reader its SELECT.
    """
    sql = " ".join(_MIGRATION.read_text().split())
    assert re.search(
        r"GRANT SELECT, INSERT, UPDATE ON public\.skill_executions TO tenant_projection_writer",
        sql,
    )
    assert "GRANT SELECT ON public.skill_executions TO app_dashboard;" in sql
