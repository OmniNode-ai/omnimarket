# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The six agent_* tables: their columns, conflict keys and INSERT statements.

Each statement is the one the retired consumer's writer ran, column for column.
Four tables keep the first row written for a key (``ON CONFLICT DO NOTHING``);
agent_execution_logs follows an execution's lifecycle (``DO UPDATE``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import BaseModel

from omnimarket.nodes.node_projection_agent_actions.models import (
    ModelAgentAction,
    ModelAgentStatusEvent,
    ModelDetectionFailure,
    ModelExecutionLog,
    ModelPerformanceMetric,
    ModelTransformationEvent,
)

_EXECUTION_LOG_LIFECYCLE = """
    status = EXCLUDED.status,
    completed_at = EXCLUDED.completed_at,
    duration_ms = EXCLUDED.duration_ms,
    exit_code = EXCLUDED.exit_code,
    error_message = EXCLUDED.error_message,
    output_summary = EXCLUDED.output_summary,
    metadata = EXCLUDED.metadata,
    project_path = COALESCE(EXCLUDED.project_path, agent_execution_logs.project_path),
    project_name = COALESCE(EXCLUDED.project_name, agent_execution_logs.project_name),
    claude_session_id = COALESCE(EXCLUDED.claude_session_id, agent_execution_logs.claude_session_id),
    terminal_id = COALESCE(EXCLUDED.terminal_id, agent_execution_logs.terminal_id),
    updated_at = NOW()
"""


@dataclass(frozen=True)
class TableSpec:
    """How one event model maps to one table."""

    table: str
    model: type[BaseModel]
    conflict_column: str
    columns: tuple[str, ...]
    json_columns: frozenset[str] = frozenset()
    # Column -> SQL expression written in place of a bound parameter.
    sql_literals: Mapping[str, str] = field(default_factory=dict)
    # Assignments of ``DO UPDATE SET``; None keeps the first row (``DO NOTHING``).
    update_assignments: str | None = None
    # True when the producer sends no ``id``, so the handler derives one.
    derives_id: bool = False

    @property
    def bound_columns(self) -> tuple[str, ...]:
        return tuple(c for c in self.columns if c not in self.sql_literals)

    def insert_statement(self) -> str:
        placeholders: list[str] = []
        bound = 0
        for column in self.columns:
            if column in self.sql_literals:
                placeholders.append(self.sql_literals[column])
                continue
            bound += 1
            cast = "::jsonb" if column in self.json_columns else ""
            placeholders.append(f"${bound}{cast}")
        action = (
            "DO NOTHING"
            if self.update_assignments is None
            else f"DO UPDATE SET {self.update_assignments}"
        )
        return (
            f"INSERT INTO {self.table} ({', '.join(self.columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({self.conflict_column}) {action}"
        )


TABLE_SPECS: Mapping[str, TableSpec] = {
    spec.table: spec
    for spec in (
        TableSpec(
            table="agent_actions",
            model=ModelAgentAction,
            conflict_column="id",
            columns=(
                "id",
                "correlation_id",
                "agent_name",
                "action_type",
                "action_name",
                "created_at",
                "status",
                "duration_ms",
                "result",
                "error_message",
                "metadata",
                "project_path",
                "project_name",
                "working_directory",
            ),
            json_columns=frozenset({"metadata"}),
            derives_id=True,
        ),
        TableSpec(
            table="agent_transformation_events",
            model=ModelTransformationEvent,
            conflict_column="id",
            columns=(
                "id",
                "correlation_id",
                "source_agent",
                "target_agent",
                "created_at",
                "trigger",
                "context",
                "metadata",
                "project_path",
                "project_name",
                "claude_session_id",
            ),
            json_columns=frozenset({"metadata"}),
        ),
        TableSpec(
            table="router_performance_metrics",
            model=ModelPerformanceMetric,
            conflict_column="id",
            columns=(
                "id",
                "metric_name",
                "metric_value",
                "created_at",
                "correlation_id",
                "unit",
                "agent_name",
                "labels",
                "metadata",
            ),
            json_columns=frozenset({"labels", "metadata"}),
        ),
        TableSpec(
            table="agent_detection_failures",
            model=ModelDetectionFailure,
            conflict_column="correlation_id",
            columns=(
                "correlation_id",
                "failure_reason",
                "created_at",
                "request_summary",
                "attempted_patterns",
                "fallback_used",
                "error_code",
                "metadata",
                "project_path",
                "project_name",
                "claude_session_id",
            ),
            json_columns=frozenset({"attempted_patterns", "metadata"}),
        ),
        TableSpec(
            table="agent_execution_logs",
            model=ModelExecutionLog,
            conflict_column="execution_id",
            columns=(
                "execution_id",
                "correlation_id",
                "agent_name",
                "status",
                "created_at",
                "updated_at",
                "started_at",
                "completed_at",
                "duration_ms",
                "exit_code",
                "error_message",
                "input_summary",
                "output_summary",
                "metadata",
                "project_path",
                "project_name",
                "claude_session_id",
                "terminal_id",
            ),
            json_columns=frozenset({"metadata"}),
            update_assignments=_EXECUTION_LOG_LIFECYCLE,
        ),
        TableSpec(
            table="agent_status_events",
            model=ModelAgentStatusEvent,
            conflict_column="id",
            columns=(
                "id",
                "correlation_id",
                "agent_name",
                "session_id",
                "state",
                "status_schema_version",
                "message",
                "progress",
                "current_phase",
                "current_task",
                "blocking_reason",
                "created_at",
                "received_at",
                "metadata",
            ),
            json_columns=frozenset({"metadata"}),
            sql_literals={"received_at": "NOW()"},
            derives_id=True,
        ),
    )
}
