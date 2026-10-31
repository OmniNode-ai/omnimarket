# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only reads of finished delegation runs for the AC3 readback (OMN-21326).

Two sources, one shape. The projection is the default: every lab host reads the
same table, populated from the delegate-skill terminals the deployed lane
publishes, so the readback needs no access to the machine that invoked the CLI.
A state root (``runs/<run_id>/{run.json,receipt.json}`` as ``onex delegate``
writes them) remains an explicit, local choice for runs whose receipts exist
only there.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_budget_readback_source import (
    ModelBudgetReadbackSource,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_budget_reading import (
    ModelBudgetReading,
)

DEPLOYED_LANE = "deployed-lane"


class BudgetSourceUnreadableError(Exception):
    """The source could not be read; the readback cannot reach a verdict."""


class ProtocolBudgetReadingSource(Protocol):
    """Finished runs whose end falls in ``[since, until)``."""

    def describe(self) -> str: ...

    def read(
        self, since: datetime, until: datetime
    ) -> Sequence[ModelBudgetReading]: ...


def _redacted(exc: Exception, dsn: str | None) -> str:
    message = str(exc)
    if dsn:
        message = message.replace(dsn, "[redacted]")
    if "postgres://" in message or "postgresql://" in message or "password=" in message:
        message = "database connection/query failed (connection details redacted)"
    return f"{type(exc).__name__}: {message}"


class ProjectionBudgetReadingSource:
    """Completed terminals in a window, from the contract-declared relation."""

    def __init__(self, source: ModelBudgetReadbackSource) -> None:
        self._source = source

    def describe(self) -> str:
        return f"projection {self._source.relation} via ${self._source.dsn_env}"

    def read(self, since: datetime, until: datetime) -> Sequence[ModelBudgetReading]:
        # The variable name is the contract's own (dsn_env), not a fixed key.
        dsn = os.environ.get(self._source.dsn_env)  # ONEX_FLAG_EXEMPT
        if not dsn or not dsn.strip():
            raise BudgetSourceUnreadableError(
                f"{self._source.dsn_env} is not set; the readback reads "
                f"{self._source.relation} (populated by {self._source.populated_by})"
            )
        from omnimarket.projection.postgres_read_database import connect_read_only

        # The relation is the contract's own, validated against a plain
        # schema.table identifier pattern when the contract is loaded, so
        # quoting it here cannot be escaped.
        schema, table = self._source.relation.split(".", 1)
        query = (
            "SELECT correlation_id::text, status, requested_timeout_seconds, "
            "execution_timeout_seconds, execution_duration_ms "
            f'FROM "{schema}"."{table}" '
            "WHERE observed_at >= %s AND observed_at < %s "
            "AND status = 'completed' ORDER BY observed_at, correlation_id"
        )
        connection: Any = None
        try:
            connection = connect_read_only(dsn)
            with connection.cursor() as cursor:
                cursor.execute(query, (since, until))
                rows = list(cursor.fetchall())
        except Exception as exc:
            raise BudgetSourceUnreadableError(_redacted(exc, dsn)) from None
        finally:
            if connection is not None:
                with contextlib.suppress(Exception):
                    connection.close()
        return [
            ModelBudgetReading(
                run_id=run_id,
                status=status,
                requested_timeout_seconds=requested,
                execution_timeout_seconds=execution_timeout,
                execution_duration_ms=duration_ms,
            )
            for run_id, status, requested, execution_timeout, duration_ms in rows
        ]


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _load(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _run_dirs_in_window(
    roots: Iterable[Path], since: datetime, until: datetime
) -> Iterable[Path]:
    for root in roots:
        for run_json in sorted(root.glob("runs/*/run.json")):
            mtime = datetime.fromtimestamp(run_json.stat().st_mtime, tz=UTC)
            if since <= mtime < until:
                yield run_json.parent


class StateRootBudgetReadingSource:
    """Deployed-lane receipts under the given ``onex delegate`` state roots."""

    def __init__(self, roots: Sequence[Path]) -> None:
        self._roots = tuple(roots)

    def describe(self) -> str:
        return "state-root " + ", ".join(str(root) for root in self._roots)

    def read(self, since: datetime, until: datetime) -> Sequence[ModelBudgetReading]:
        missing = [str(root) for root in self._roots if not root.is_dir()]
        if missing:
            raise BudgetSourceUnreadableError(
                f"state root not found: {', '.join(missing)}"
            )
        readings: list[ModelBudgetReading] = []
        for run_dir in _run_dirs_in_window(self._roots, since, until):
            receipt = _load(run_dir / "receipt.json")
            if receipt is None or receipt.get("locus") != DEPLOYED_LANE:
                continue
            result = (receipt.get("receipt") or {}).get("result") or {}
            terminal = result.get("terminal_payload")
            if isinstance(terminal, dict) and isinstance(terminal.get("payload"), dict):
                result = terminal["payload"]
            budget = result.get("budget_evidence") or {}
            readings.append(
                ModelBudgetReading(
                    run_id=run_dir.name,
                    status=result.get("status")
                    if isinstance(result.get("status"), str)
                    else None,
                    requested_timeout_seconds=_int_or_none(
                        budget.get("requested_timeout_seconds")
                    ),
                    execution_timeout_seconds=_int_or_none(
                        budget.get("execution_timeout_seconds")
                    ),
                    execution_duration_ms=_int_or_none(
                        result.get("execution_duration_ms")
                    ),
                )
            )
        return readings


__all__ = [
    "BudgetSourceUnreadableError",
    "ProjectionBudgetReadingSource",
    "ProtocolBudgetReadingSource",
    "StateRootBudgetReadingSource",
]
