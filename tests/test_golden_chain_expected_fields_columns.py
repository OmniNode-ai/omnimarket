# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13544: guard that every ``expected_fields`` entry in ``golden_chains.yaml``
names a real projection-table column.

The golden-chain sweep validates a projected DB row by checking
``field not in row`` for each ``expected_fields`` entry (see
``handler_golden_chain_sweep._validate_chain``). The row keys are the columns of
the projection table named by ``tail_table``. If ``expected_fields`` references a
column that never existed in the table, the chain can only ever FAIL — that is
config drift, not a real regression signal.

Five OMN-12660 WS-G error chains drifted this way: they referenced event-payload
field names (``base_url``, ``model``, ``content``, ``model_used``, ``node_name``,
``contract_passed``, ``node_startup_ok``, ``published_at``) that are not columns
of ``delegation_events``. OMN-13544 reconciled them to the real columns.

The canonical column sets below are transcribed from the deployed forward
migrations in ``omnibase_infra``:
  - delegation_events:        docker/migrations/forward/nodes/node_projection_delegation/*.sql
  - agent_routing_decisions:  docker/migrations/forward/nodes/node_projection_routing_decision/*.sql
  - pattern_learning_artifacts: docker/migrations/forward/nodes/node_projection_pattern_learning/*.sql
  - llm_routing_decisions:    docker/migrations/forward/nodes/node_projection_llm_routing/*.sql
  - session_outcomes:         docker/migrations/forward/nodes/node_projection_session_outcome/*.sql

omnimarket tests cannot reach the omnibase_infra migration tree, so the column
sets are pinned here. If a projection table legitimately gains a column, add it
here in the same change that adds the golden-chain expectation.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

GOLDEN_CHAINS_YAML = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_golden_chain_sweep/golden_chains.yaml"
)

# Real columns per projection table (see module docstring for migration sources).
PROJECTION_TABLE_COLUMNS: dict[str, set[str]] = {
    "delegation_events": {
        "id",
        "correlation_id",
        "session_id",
        "timestamp",
        "task_type",
        "delegated_to",
        "model_name",
        "delegated_by",
        "quality_gate_passed",
        "quality_gates_checked",
        "quality_gates_failed",
        "quality_gates_checked_jsonb",
        "quality_gates_failed_jsonb",
        "quality_gate_detail",
        "cost_usd",
        "cost_savings_usd",
        "delegation_latency_ms",
        "latency_ms",
        "repo",
        "is_shadow",
        "llm_call_id",
        "prompt_text",
        "response_text",
        "tokens_input",
        "tokens_output",
        "tokens_to_compliance",
        "compliance_attempts",
        "pricing_manifest_version",
        "created_at",
        "context_pack_hash",
        "projection_version",
        "reducer_version",
        "escalation_count",
        "cost_tier_name",
        "cost_tier_type",
        "cost_measurement_source",
        "budget_headroom_consumed_usd",
        "authority_source",
        "premium_counterfactual",
        "override_within_bounds",
        "request_override_applied",
        "required_bar",
        "actual_score",
        "score_source",
    },
    "agent_routing_decisions": {
        "id",
        "correlation_id",
        "claude_session_id",
        "request_type",
        "selected_agent",
        "confidence_score",
        "routing_reason",
        "alternatives",
        "domain",
        "project_name",
        "project_path",
        "metadata",
        "created_at",
    },
    "pattern_learning_artifacts": {
        "id",
        "correlation_id",
        "pattern_id",
        "pattern_name",
        "pattern_type",
        "language",
        "signature",
        "composite_score",
        "lifecycle_state",
        "scoring_evidence",
        "metrics",
        "metadata",
        "projected_at",
        "state_changed_at",
        "created_at",
        "updated_at",
    },
    "llm_routing_decisions": {
        "id",
        "correlation_id",
        "session_id",
        "intent",
        "fuzzy_agent",
        "fuzzy_confidence",
        "fuzzy_latency_ms",
        "llm_agent",
        "llm_confidence",
        "llm_latency_ms",
        "agreement",
        "used_fallback",
        "model",
        "cost_usd",
        "routing_prompt_version",
        "projected_at",
        "created_at",
    },
    "session_outcomes": {
        "session_id",
        "outcome",
        "emitted_at",
        "ingested_at",
        "created_at",
        "updated_at",
    },
}


def _load_chains() -> list[dict[str, object]]:
    data = yaml.safe_load(GOLDEN_CHAINS_YAML.read_text())
    return list(data["chains"])


@pytest.mark.unit
class TestGoldenChainExpectedFieldsAreRealColumns:
    def test_every_expected_field_is_a_real_projection_column(self) -> None:
        drift: list[str] = []
        for chain in _load_chains():
            tail_table = str(chain["tail_table"])
            # event_bus:<topic> tail tables project onto the bus payload, not a
            # Postgres table, so column parity does not apply.
            if tail_table.startswith("event_bus:"):
                continue
            known = PROJECTION_TABLE_COLUMNS.get(tail_table)
            assert known is not None, (
                f"chain {chain['name']!r} targets unknown projection table "
                f"{tail_table!r}; add its column set to "
                f"PROJECTION_TABLE_COLUMNS (sourced from the node migration) "
                f"before declaring expected_fields against it."
            )
            for field in chain.get("expected_fields", []) or []:
                if field not in known:
                    drift.append(f"{chain['name']}.{field} (table {tail_table})")
        assert not drift, (
            "golden_chains.yaml expected_fields reference columns that do not "
            "exist in the projection table (config drift):\n  " + "\n  ".join(drift)
        )
