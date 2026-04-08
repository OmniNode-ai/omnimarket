"""HandlerDataVerification — post-pipeline data verification compute node.

Deterministic verification: query sample rows, run data quality checks,
return structured pass/fail result. Works with both mock data (testing)
and real DB queries (runtime).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

from omnibase_compat.protocols import ProtocolDataSource as DataSource

from omnimarket.nodes.node_data_verification.models.model_data_verification_completed_event import (
    ModelDataVerificationCompletedEvent,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_start_command import (
    ModelDataVerificationStartCommand,
)
from omnimarket.nodes.node_data_verification.models.model_data_verification_state import (
    EnumDataCheck,
    EnumVerificationStatus,
    ModelDataVerificationResult,
    ModelSampleRow,
)

logger = logging.getLogger(__name__)


# UUID v4 pattern — rejects nil UUID and sequential/default UUIDs
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Known garbage UUIDs (nil, sequential defaults)
_GARBAGE_UUIDS = frozenset(
    {
        "00000000-0000-0000-0000-000000000000",
        "00000000-0000-0000-0000-000000000001",
        "11111111-1111-1111-1111-111111111111",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    }
)


class InmemoryDataSource:
    """Mock data source for testing."""

    def __init__(self, rows: list[dict[str, str]] | None = None) -> None:
        self._rows = rows or []

    def get_row_count(self, table_name: str) -> int:
        return len(self._rows)

    def get_sample_rows(
        self, table_name: str, sample_size: int
    ) -> list[dict[str, str]]:
        return self._rows[:sample_size]

    def get_columns(self, table_name: str) -> list[str]:
        if not self._rows:
            return []
        return list(self._rows[0].keys())


def _is_garbage_uuid(value: str) -> bool:
    """Check if a UUID string is garbage (nil, sequential, or non-v4)."""
    lower = value.strip().lower()
    if lower in _GARBAGE_UUIDS:
        return True
    if not _UUID_V4_RE.match(lower):
        return True
    return False


class HandlerDataVerification:
    """Handler for post-pipeline data verification.

    Pure logic with injectable data source for testability.
    """

    def _check_row(
        self,
        row_index: int,
        row: dict[str, str],
        expected_columns: list[str],
        unique_columns: list[str],
        uuid_columns: list[str],
        seen_unique_values: dict[str, set[str]],
    ) -> ModelSampleRow:
        """Run all per-row checks and return a ModelSampleRow."""
        passed: list[EnumDataCheck] = []
        failed: list[EnumDataCheck] = []
        issues: list[str] = []

        # NO_NULL_REQUIRED_FIELDS
        null_cols = [
            c
            for c in expected_columns
            if c in row and (row[c] == "" or row[c] == "None" or row[c] == "null")
        ]
        missing_cols = [c for c in expected_columns if c not in row]
        if null_cols or missing_cols:
            failed.append(EnumDataCheck.NO_NULL_REQUIRED_FIELDS)
            for c in null_cols:
                issues.append(f"Column '{c}' is null/empty in row {row_index}")
            for c in missing_cols:
                issues.append(f"Column '{c}' missing from row {row_index}")
        else:
            passed.append(EnumDataCheck.NO_NULL_REQUIRED_FIELDS)

        # NO_GARBAGE_UUIDS
        garbage_cols = [
            c for c in uuid_columns if c in row and _is_garbage_uuid(row[c])
        ]
        if garbage_cols:
            failed.append(EnumDataCheck.NO_GARBAGE_UUIDS)
            for c in garbage_cols:
                issues.append(
                    f"Column '{c}' has garbage UUID '{row[c]}' in row {row_index}"
                )
        else:
            passed.append(EnumDataCheck.NO_GARBAGE_UUIDS)

        # NO_DUPLICATES (accumulate seen values)
        dup_cols: list[str] = []
        for c in unique_columns:
            if c in row:
                val = row[c]
                if c not in seen_unique_values:
                    seen_unique_values[c] = set()
                if val in seen_unique_values[c]:
                    dup_cols.append(c)
                seen_unique_values[c].add(val)
        if dup_cols:
            failed.append(EnumDataCheck.NO_DUPLICATES)
            for c in dup_cols:
                issues.append(
                    f"Column '{c}' has duplicate value '{row[c]}' in row {row_index}"
                )
        else:
            passed.append(EnumDataCheck.NO_DUPLICATES)

        return ModelSampleRow(
            row_index=row_index,
            data=row,
            checks_passed=passed,
            checks_failed=failed,
            issues=issues,
        )

    def verify(
        self,
        command: ModelDataVerificationStartCommand,
        data_source: DataSource,
        event_landed: bool | None = None,
        latency_ms: float | None = None,
    ) -> ModelDataVerificationResult:
        """Run verification checks against the data source."""
        table_name = command.table_name
        issues: list[str] = []
        checks_summary: dict[str, int] = {check.value: 0 for check in EnumDataCheck}

        # ROW_COUNT_NONZERO
        total_rows = data_source.get_row_count(table_name)
        if total_rows >= command.min_rows:
            checks_summary[EnumDataCheck.ROW_COUNT_NONZERO] = 1
        else:
            issues.append(f"Row count {total_rows} below minimum {command.min_rows}")

        # SCHEMA_MATCH
        columns = data_source.get_columns(table_name)
        missing_schema = [c for c in command.expected_columns if c not in columns]
        if not missing_schema and command.expected_columns:
            checks_summary[EnumDataCheck.SCHEMA_MATCH] = 1
        elif missing_schema:
            issues.append(f"Missing columns: {missing_schema}")
        elif not command.expected_columns:
            # No expected columns specified — skip schema check
            checks_summary[EnumDataCheck.SCHEMA_MATCH] = 1

        # EVENT_LANDED
        if event_landed is not None:
            if event_landed:
                checks_summary[EnumDataCheck.EVENT_LANDED] = 1
            else:
                issues.append("Test event did not land in the database")

        # Sample rows and run per-row checks
        sample_rows_data = data_source.get_sample_rows(table_name, command.sample_size)
        seen_unique_values: dict[str, set[str]] = {}
        sample_rows: list[ModelSampleRow] = []

        for idx, row in enumerate(sample_rows_data):
            sample_row = self._check_row(
                row_index=idx,
                row=row,
                expected_columns=command.expected_columns,
                unique_columns=command.unique_columns,
                uuid_columns=command.uuid_columns,
                seen_unique_values=seen_unique_values,
            )
            sample_rows.append(sample_row)
            issues.extend(sample_row.issues)

            # Accumulate per-row check passes
            for check in sample_row.checks_passed:
                checks_summary[check.value] += 1

        # Determine overall status
        has_row_count_fail = total_rows < command.min_rows
        has_event_fail = event_landed is False
        has_row_issues = any(r.checks_failed for r in sample_rows)

        if has_row_count_fail and total_rows == 0 and not sample_rows:
            status = EnumVerificationStatus.FAIL
        elif has_row_count_fail or has_event_fail or has_row_issues:
            # Some checks passed, some failed
            any_pass = any(r.checks_passed for r in sample_rows) or (
                total_rows >= command.min_rows
            )
            if any_pass and (has_row_issues or has_row_count_fail or has_event_fail):
                status = EnumVerificationStatus.PARTIAL
            else:
                status = EnumVerificationStatus.FAIL
        else:
            status = EnumVerificationStatus.PASS

        return ModelDataVerificationResult(
            table_name=table_name,
            status=status,
            total_rows=total_rows,
            sample_rows=sample_rows,
            checks_summary=checks_summary,
            issues=issues,
            event_landed=event_landed,
            latency_ms=latency_ms,
            correlation_id=command.correlation_id,
            dry_run=command.dry_run,
        )

    def handle(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Delegates to run_verification with a ModelDataVerificationStartCommand
        and an InmemoryDataSource constructed from input_data.
        """
        rows = input_data.pop("rows", [])
        event_landed = input_data.pop("event_landed", None)
        latency_ms = input_data.pop("latency_ms", None)
        command = ModelDataVerificationStartCommand(**input_data)
        data_source = InmemoryDataSource(rows=rows)
        result, _completed = self.run_verification(
            command, data_source, event_landed=event_landed, latency_ms=latency_ms
        )
        return result.model_dump(mode="json")

    def make_completed_event(
        self,
        result: ModelDataVerificationResult,
        started_at: datetime,
    ) -> ModelDataVerificationCompletedEvent:
        """Create a completion event from the verification result."""
        return ModelDataVerificationCompletedEvent(
            correlation_id=result.correlation_id,
            table_name=result.table_name,
            status=result.status,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            result=result,
        )

    def serialize_completed(self, event: ModelDataVerificationCompletedEvent) -> bytes:
        """Serialize a completed event to bytes."""
        return json.dumps(event.model_dump(mode="json")).encode()

    def run_verification(
        self,
        command: ModelDataVerificationStartCommand,
        data_source: DataSource,
        event_landed: bool | None = None,
        latency_ms: float | None = None,
    ) -> tuple[ModelDataVerificationResult, ModelDataVerificationCompletedEvent]:
        """Run a complete verification cycle.

        Deterministic entry point for testing.
        """
        started_at = datetime.now(tz=UTC)
        result = self.verify(
            command, data_source, event_landed=event_landed, latency_ms=latency_ms
        )
        completed = self.make_completed_event(result, started_at)
        return result, completed


__all__: list[str] = ["DataSource", "HandlerDataVerification", "InmemoryDataSource"]
