# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's migrations are the ones omnibase_infra vendors (OMN-26708).

The vendored copy creates one relation, ``lane_work_workflow``, and no view; the
terminal rows are a contract query. A node migration that adds a view diverges
from the vendored file of the same name, so the composed runtime reads two
different migrations under one name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_lane_work import contract_topics

pytestmark = pytest.mark.unit

_MIGRATIONS = Path(contract_topics.CONTRACT_PATH).parent / "migrations"


def test_the_node_migrations_create_no_view() -> None:
    sql = "\n".join(p.read_text(encoding="utf-8") for p in _MIGRATIONS.glob("*.sql"))
    assert "CREATE TABLE IF NOT EXISTS omninode_internal.lane_work_workflow" in sql
    assert "CREATE OR REPLACE VIEW" not in sql.upper()
    assert "CREATE VIEW" not in sql.upper()


def test_both_exposures_read_the_one_relation() -> None:
    assert (
        contract_topics.TOPIC_EXPOSURE_WORKFLOW
        != contract_topics.TOPIC_EXPOSURE_TERMINAL
    )
    assert {e.table for e in contract_topics._EXPOSURES} == {"lane_work_workflow"}
