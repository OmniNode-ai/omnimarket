# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tenant savings totals exclude the uncounted data sources declared in the node contract."""

from __future__ import annotations

import pytest

from omnimarket.handlers.tenant_projection_read.projection_reader import (
    RECENT_DELEGATIONS_FALLBACK_SQL,
    RECENT_DELEGATIONS_SQL,
    SAVINGS_SQL,
    AnalyticsTenantProjectionReader,
)
from omnimarket.handlers.tenant_projection_read.resolver_reader import reader_for_node
from omnimarket.projection.envelope import DATA_SOURCE_EXPERIMENT
from tests.nodes.tenant_read_support import (
    TENANT_A,
    TypedAnalyticsStub,
    flat,
)

pytestmark = pytest.mark.unit


class _FakeResolver:
    async def get_secret_async(self, name: str) -> None:
        return None


def _assert_savings_sql_excludes_uncounted(sql: str) -> None:
    flat_sql = flat(sql)
    assert (
        flat_sql.count(
            "FROM delegation_events WHERE tenant_id = %(tenant_uuid)s AND data_source <> ALL(%(uncounted_sources)s::text[])"
        )
        == 2
    )
    assert (
        flat_sql.count(
            "AND NOT EXISTS (SELECT 1 FROM delegation_events d WHERE d.correlation_id = savings_estimates.session_id AND d.tenant_id = %(tenant_uuid)s AND d.data_source = ANY(%(uncounted_sources)s::text[]))"
        )
        == 2
    )


async def test_read_savings_binds_uncounted_sources_as_a_list() -> None:
    stub = TypedAnalyticsStub(rows=[])
    reader = AnalyticsTenantProjectionReader(
        stub, uncounted_data_sources=("experiment",)
    )
    await reader.read_savings(TENANT_A)
    _sql, params, _rls = stub.calls[0]
    assert params["uncounted_sources"] == ["experiment"]
    assert params["tenant_uuid"] == TENANT_A
    assert params["tenant_text"] == str(TENANT_A)


def test_savings_sql_excludes_uncounted_sources_and_the_list_queries_do_not() -> None:
    _assert_savings_sql_excludes_uncounted(SAVINGS_SQL)
    assert "uncounted_sources" not in flat(RECENT_DELEGATIONS_SQL)
    assert "uncounted_sources" not in flat(RECENT_DELEGATIONS_FALLBACK_SQL)


def test_reader_for_node_binds_the_contract_declared_uncounted_sources() -> None:
    reader = reader_for_node(
        "omnimarket.nodes.node_tenant_savings_read_effect", _FakeResolver()
    )
    assert reader is not None
    assert reader._uncounted_data_sources == (DATA_SOURCE_EXPERIMENT,)
    assert list(reader._uncounted_data_sources) == ["experiment"]

    reader_delegations = reader_for_node(
        "omnimarket.nodes.node_tenant_delegations_read_effect", _FakeResolver()
    )
    assert reader_delegations is not None
    assert reader_delegations._uncounted_data_sources == (DATA_SOURCE_EXPERIMENT,)


def test_the_base_revision_sql_lacks_the_exclusion_predicates() -> None:
    base_savings_sql = """
    SELECT
      COALESCE(
        (SELECT SUM(savings_usd) FROM savings_estimates
         WHERE tenant_id = %(tenant_text)s), 0
      )::float8 AS total_saved_usd,
      COALESCE(
        (SELECT SUM(local_cost_usd) FROM savings_estimates
         WHERE tenant_id = %(tenant_text)s), 0
      )::float8 AS total_spend_usd,
      COALESCE(
        (SELECT COUNT(*) FROM delegation_events
         WHERE tenant_id = %(tenant_uuid)s), 0
      )::bigint AS delegation_runs,
      COALESCE(
        (SELECT SUM(tokens_input + tokens_output) FROM delegation_events
         WHERE tenant_id = %(tenant_uuid)s), 0
      )::bigint AS total_tokens,
      (SELECT MAX(event_timestamp) FROM savings_estimates
       WHERE tenant_id = %(tenant_text)s) AS savings_latest,
      (SELECT MAX(timestamp) FROM delegation_events
       WHERE tenant_id = %(tenant_uuid)s) AS runs_latest
"""
    with pytest.raises(AssertionError):
        _assert_savings_sql_excludes_uncounted(base_savings_sql)


def test_a_reader_with_no_uncounted_sources_is_refused() -> None:
    """An empty list would count every run: the reader refuses it instead of guessing."""
    with pytest.raises(ValueError, match="uncounted_data_sources is empty"):
        AnalyticsTenantProjectionReader(
            TypedAnalyticsStub(rows=[]), uncounted_data_sources=()
        )
