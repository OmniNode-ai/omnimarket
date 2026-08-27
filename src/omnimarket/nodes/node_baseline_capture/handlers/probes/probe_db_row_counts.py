"""DB row count probe — queries key projection tables from Postgres.

Tenant seam (OMN-15797 AC3): ``delegation_events`` is RLS-covered
(``node_projection_delegation`` migration ``0023_delegation_rls_tenant_
isolation.sql``). With ``app.tenant_id`` unset the policy predicate is NULL,
so ``SELECT COUNT(*)`` returns **0** without raising — and this probe would
then record ``row_count: 0`` in a baseline snapshot as though the table were
empty. A baseline that silently reports zero is worse than no baseline: it is
the OMN-15797 defect wearing a measurement's clothes. So each count runs
inside its own tenant-scoped transaction.
"""

from __future__ import annotations

import logging
import os

from omnimarket.nodes.node_baseline_capture.models.model_baseline import (
    ModelDbRowCountSnapshot,
    ProbeSnapshotItem,
)
from omnimarket.projection.tenant_isolation import (
    TENANT_GUC,
    resolve_rls_read_tenant,
)

logger = logging.getLogger(__name__)

_KEY_TABLES = [
    "session_outcomes",
    "delegation_events",
    "llm_cost_events",
    "registration_events",
    "savings_events",
    "baseline_snapshots",
    "log_events",
]


class ProbeDbRowCounts:
    """Probe that queries row counts for key projection tables."""

    name: str = "db_row_counts"

    async def collect(self, omni_home: str) -> list[ProbeSnapshotItem]:
        """Query Postgres for row counts using asyncpg.

        Reads OMNIBASE_INFRA_DB_URL from environment. Returns empty list on failure.
        """
        try:
            import asyncpg
        except ImportError:
            logger.warning("asyncpg not available — skipping db_row_counts probe")
            return []

        db_url = os.environ.get("OMNIBASE_INFRA_DB_URL", "")  # contract-config-ok: config  # fmt: skip
        if not db_url:
            logger.warning(
                "OMNIBASE_INFRA_DB_URL not set — skipping db_row_counts probe"
            )
            return []

        # Resolved BEFORE connecting: an unresolvable tenant under
        # ENFORCE_TENANT_ISOLATION raises rather than producing a snapshot full
        # of zeros. Deliberately outside the connect try/except below — that
        # one is fail-open for a telemetry OUTAGE, and a blinded count is a
        # correctness defect, not an outage.
        tenant = resolve_rls_read_tenant(None, table="db_row_counts")

        try:
            conn = await asyncpg.connect(db_url, timeout=5.0)
        except Exception as exc:
            logger.warning("Could not connect to Postgres: %s", exc)
            return []

        results: list[ProbeSnapshotItem] = []
        try:
            for table in _KEY_TABLES:
                try:
                    # One transaction PER table, not one around the loop: the
                    # per-table failure below is non-fatal by design, and a
                    # statement error inside a shared transaction would abort
                    # it and fail every remaining table too. set_config's
                    # is_local=true also means the GUC must be set inside the
                    # same transaction as the count, not once at connect —
                    # under autocommit each statement is its own transaction,
                    # so a session-level attempt would evaporate before the
                    # COUNT ran (the OMN-15306 silent no-op shape).
                    async with conn.transaction():
                        await conn.execute(
                            "SELECT set_config($1, $2, true)", TENANT_GUC, tenant
                        )
                        row = await conn.fetchrow(
                            f"SELECT COUNT(*) AS cnt FROM {table}"
                        )
                    count = int(row["cnt"]) if row else 0
                    results.append(
                        ModelDbRowCountSnapshot(
                            table_name=table,
                            row_count=count,
                        )
                    )
                except Exception as exc:
                    logger.warning(
                        "Row count query failed for table %s: %s", table, exc
                    )
                    # Non-fatal: skip this table
        finally:
            await conn.close()

        return results


__all__: list[str] = ["ProbeDbRowCounts"]
