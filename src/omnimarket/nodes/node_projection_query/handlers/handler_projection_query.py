"""HandlerProjectionQuery — read-only query shapes for omnidash dashboards.

Exposes 10 query shapes that read from omnidash_analytics tables.
Omnidash calls this node via HTTP instead of querying Postgres directly,
enforcing the projection-only architecture (OMN-8899).

Command topic: onex.cmd.omnimarket.projection-query-requested.v1
Response topic: onex.evt.omnimarket.projection-query-completed.v1
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import cast

from omnimarket.projection.protocol_database import DatabaseAdapter

SUPPORTED_SHAPES: list[str] = [
    "staleness",
    "projection-health",
    "system-activity",
    "hot-nodes",
    "intent-drift",
    "contract-drift",
    "model-efficiency",
    "dlq",
    "eval-results",
    "intent-breakdown",
]

STALENESS_FEATURE_TABLES: dict[str, tuple[str, str]] = {
    "patterns": ("pattern_learning_artifacts", "projected_at"),
    "enforcement": ("pattern_enforcement_events", "created_at"),
    "effectiveness": ("injection_effectiveness", "created_at"),
    "rl-episodes": ("rl_episodes", "created_at"),
    "llm-routing": ("llm_routing_decisions", "created_at"),
    "intent-signals": ("intent_signals", "created_at"),
    "session-outcomes": ("session_outcomes", "created_at"),
    "latency-breakdowns": ("latency_breakdowns", "created_at"),
    "enrichment": ("context_enrichment_events", "created_at"),
    "delegation": ("delegation_events", "created_at"),
    "gate-decisions": ("gate_decisions", "created_at"),
    "epic-runs": ("epic_run_events", "created_at"),
    "compliance": ("compliance_evaluations", "created_at"),
}


class HandlerProjectionQuery:
    """Read-only query handler for 10 dashboard query shapes."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        db_raw = input_data.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        shape = str(input_data.get("shape", ""))
        raw_params = input_data.get("params") or {}
        params = {str(k): v for k, v in cast(dict[str, object], raw_params).items()}
        return self.query(shape, params, db_raw)

    def query(
        self,
        shape: str,
        params: dict[str, object],
        db: DatabaseAdapter,
    ) -> dict[str, object]:
        if shape not in SUPPORTED_SHAPES:
            raise ValueError(f"Unsupported query shape: {shape!r}")

        dispatch: dict[str, object] = {
            "staleness": self._query_staleness,
            "projection-health": self._query_projection_health,
            "system-activity": self._query_system_activity,
            "hot-nodes": self._query_hot_nodes,
            "intent-drift": self._query_intent_drift,
            "contract-drift": self._query_contract_drift,
            "model-efficiency": self._query_model_efficiency,
            "dlq": self._query_dlq,
            "eval-results": self._query_eval_results,
            "intent-breakdown": self._query_intent_breakdown,
        }

        handler_fn = dispatch[shape]
        data = handler_fn(params, db)  # type: ignore[operator]

        return {
            "shape": shape,
            "data": data,
            "queried_at": datetime.now(tz=UTC).isoformat(),
        }

    # ------------------------------------------------------------------
    # Shape implementations
    # ------------------------------------------------------------------

    def _query_staleness(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        features: dict[str, object] = {}
        for name, (table, ts_col) in STALENESS_FEATURE_TABLES.items():
            rows = db.query(table)
            last_updated = None
            if rows:
                timestamps: list[str] = [str(r[ts_col]) for r in rows if r.get(ts_col)]
                if timestamps:
                    last_updated = max(timestamps)
            features[name] = {
                "name": name,
                "last_updated": last_updated,
                "stale": last_updated is None,
            }
        return {"features": features, "checked_at": datetime.now(tz=UTC).isoformat()}

    def _query_projection_health(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        tables_to_check = [
            "agent_routing_decisions",
            "agent_actions",
            "pattern_learning_artifacts",
            "injection_effectiveness",
            "intent_signals",
            "delegation_events",
            "session_outcomes",
            "dlq_messages",
        ]
        tables: dict[str, object] = {}
        populated = 0
        for table_name in tables_to_check:
            rows = db.query(table_name)
            count = len(rows)
            if count > 0:
                populated += 1
            tables[table_name] = {"row_count": count}

        watermarks = db.query("projection_watermarks")

        return {
            "tables": tables,
            "watermarks": watermarks,
            "summary": {
                "total_tables": len(tables),
                "populated_tables": populated,
                "empty_tables": len(tables) - populated,
            },
        }

    def _query_system_activity(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        build_loop = db.query("phase_metrics_events")
        pipelines = db.query("skill_invocations")
        sessions = db.query("session_outcomes")
        delegations = db.query("delegation_events")

        return {
            "build_loop": build_loop,
            "pipelines": pipelines,
            "sessions": sessions,
            "delegations": delegations,
        }

    def _query_hot_nodes(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        rows = db.query("agent_routing_decisions")
        counter: Counter[str] = Counter()
        last_seen: dict[str, str] = {}
        for row in rows:
            agent = str(row.get("selected_agent", "unknown"))
            counter[agent] += 1
            ts = str(row.get("created_at", ""))
            if ts > last_seen.get(agent, ""):
                last_seen[agent] = ts

        nodes = [
            {
                "node_id": agent,
                "event_count": count,
                "last_seen": last_seen.get(agent),
                "rank": rank,
            }
            for rank, (agent, count) in enumerate(counter.most_common(20), 1)
        ]
        return {"nodes": nodes}

    def _query_intent_drift(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        recent = db.query("intent_drift_events")
        severity_counter: Counter[str] = Counter()
        for row in recent:
            severity_counter[str(row.get("severity", "unknown"))] += 1

        summary = [
            {"severity": sev, "count": cnt}
            for sev, cnt in severity_counter.most_common()
        ]
        return {"recent": recent, "summary": summary}

    def _query_contract_drift(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        recent = db.query("contract_drift_events")

        severity_counter: Counter[str] = Counter()
        type_counter: Counter[str] = Counter()
        for row in recent:
            severity_counter[str(row.get("severity", "unknown"))] += 1
            type_counter[str(row.get("drift_type", "unknown"))] += 1

        return {
            "recent": recent,
            "by_severity": [
                {"severity": s, "count": c} for s, c in severity_counter.most_common()
            ],
            "by_type": [
                {"drift_type": t, "count": c} for t, c in type_counter.most_common()
            ],
        }

    def _query_model_efficiency(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        all_rows = db.query("model_efficiency_rollups")
        final_rows = [r for r in all_rows if r.get("rollup_status") == "final"]

        model_groups: dict[str, list[dict[str, object]]] = {}
        for row in final_rows:
            mid = str(row.get("model_id", "unknown"))
            model_groups.setdefault(mid, []).append(row)

        summary = []
        for model_id, rows in model_groups.items():
            vts_values = [float(cast(int | float, r.get("vts") or 0)) for r in rows]
            summary.append(
                {
                    "model_id": model_id,
                    "rollup_status": "final",
                    "pr_count": len(rows),
                    "avg_vts": sum(vts_values) / len(vts_values) if vts_values else 0,
                    "total_blocking_failures": sum(
                        int(cast(int | float, r.get("blocking_failures") or 0))
                        for r in rows
                    ),
                }
            )

        return {"summary": summary}

    def _query_dlq(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        messages = db.query("dlq_messages")

        error_counter: Counter[str] = Counter()
        for msg in messages:
            error_counter[str(msg.get("error_type", "unknown"))] += 1

        return {
            "messages": messages,
            "error_breakdown": [
                {"error_type": et, "count": c} for et, c in error_counter.most_common()
            ],
            "total": len(messages),
        }

    def _query_eval_results(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        reports = db.query("eval_reports")
        return {"reports": reports}

    def _query_intent_breakdown(
        self, params: dict[str, object], db: DatabaseAdapter
    ) -> dict[str, object]:
        rows = db.query("intent_signals")
        type_counter: Counter[str] = Counter()
        for row in rows:
            type_counter[str(row.get("intent_type", "unknown"))] += 1

        breakdown = [
            {"intent_type": t, "count": c} for t, c in type_counter.most_common()
        ]
        return {"breakdown": breakdown}


__all__: list[str] = [
    "SUPPORTED_SHAPES",
    "HandlerProjectionQuery",
]
