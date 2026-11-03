# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_projection_local_alerts' Postgres migrations, read statically (OMN-19991).

The table holds one row per (tenant, code, subject), so a repeated condition
updates one row; its columns are the ones the fold plan writes; the tenant
projection writer may read, insert and update but not delete; row-level
security is enabled with the tenant policy in 0000 and forced in 0002.
Failure modes: a missing file, a key other than (tenant_id, code, subject), a
column the fold writes that the table lacks, a DELETE grant, or RLS missing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.enums.enum_local_alert_severity import EnumLocalAlertSeverity
from omnimarket.enums.enum_local_alert_transition import EnumLocalAlertTransition
from omnimarket.enums.enum_local_environment_mode import EnumLocalEnvironmentMode
from omnimarket.enums.enum_local_service_verdict import EnumLocalServiceVerdict
from omnimarket.models.model_local_alert_transitioned import (
    ModelLocalAlertEvidence,
    ModelLocalAlertTransitioned,
)
from omnimarket.nodes.node_local_alert_transition_compute.handlers.handler_local_alert_transition import (
    LOCAL_ALERTS_KEY,
    plan_local_alert_fold,
)

pytestmark = pytest.mark.unit

MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_alerts/migrations"
)
CREATE = MIGRATIONS / "0000_create_local_alerts.sql"
GRANT = MIGRATIONS / "0001_grant_tenant_projection_writer_local_alerts.sql"
FORCE = MIGRATIONS / "0002_force_rls_local_alerts.sql"


def _create_body() -> str:
    text = CREATE.read_text()
    match = re.search(
        r"CREATE TABLE IF NOT EXISTS public\.local_alerts \((.*?)\n\);", text, re.S
    )
    assert match, "0000 does not create public.local_alerts"
    return match.group(1)


def _columns() -> set[str]:
    names = set()
    for line in _create_body().splitlines():
        line = line.strip()
        if not line or line.startswith(("--", "PRIMARY", "CONSTRAINT", "(", ")")):
            continue
        token = line.split()[0]
        if token.isidentifier() and token.islower():
            names.add(token)
    return names


def test_the_three_migrations_exist() -> None:
    for path in (CREATE, GRANT, FORCE):
        assert path.is_file(), f"missing {path.name}"


def test_key_is_tenant_code_subject() -> None:
    assert "PRIMARY KEY (tenant_id, code, subject)" in _create_body()
    assert LOCAL_ALERTS_KEY == "tenant_id,code,subject"


def test_table_has_every_column_the_fold_writes() -> None:
    at = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)
    transition = ModelLocalAlertTransitioned(
        tenant_id="11111111-1111-4111-8111-111111111111",
        code="SERVICE_DOWN",
        subject="compose/postgres",
        transition=EnumLocalAlertTransition.RAISED,
        severity=EnumLocalAlertSeverity.CRITICAL,
        raised_at=at,
        occurred_at=at,
        evidence=ModelLocalAlertEvidence(
            mode=EnumLocalEnvironmentMode.COMPOSE,
            service="postgres",
            verdict=EnumLocalServiceVerdict.DOWN,
            reason="exited",
            last_seen=at,
        ),
    )
    planned = plan_local_alert_fold(transition, None)
    assert planned is not None
    missing = set(planned) - _columns()
    assert not missing, f"local_alerts lacks fold columns {sorted(missing)}"
    assert {"updated_at", "projection_cursor"} <= _columns()


def test_state_and_clear_time_agree() -> None:
    body = _create_body()
    assert "state IN ('raised', 'cleared')" in body
    assert "(state = 'cleared') = (cleared_at IS NOT NULL)" in body


def test_rls_enabled_with_tenant_policy_in_0000_and_forced_in_0002() -> None:
    policy = "USING (tenant_id = current_setting('app.tenant_id', true))"
    create = CREATE.read_text()
    assert "ALTER TABLE public.local_alerts ENABLE ROW LEVEL SECURITY;" in create
    assert policy in create
    assert "FORCE ROW LEVEL SECURITY" not in create
    force = FORCE.read_text()
    assert "ALTER TABLE public.local_alerts FORCE ROW LEVEL SECURITY;" in force
    assert policy in force


def test_writer_reads_inserts_updates_and_never_deletes() -> None:
    grant = GRANT.read_text()
    assert re.search(
        r"GRANT SELECT, INSERT, UPDATE\s+ON public\.local_alerts\s+TO tenant_projection_writer;",
        grant,
    )
    assert "DELETE" not in re.sub(r"--[^\n]*", "", grant)
    assert "ON SEQUENCE public.local_alerts_projection_cursor_seq" in grant
