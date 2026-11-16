# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Two review findings on the skill lifecycle projection (OMN-21933).

1. Migration 0001 adds ``received_at`` with ``DEFAULT NOW()`` only through
   ``ADD COLUMN IF NOT EXISTS``, a no-op when the column already exists. The writer's
   INSERT omits ``received_at``, so a pre-existing column without a default broke every
   insert. 0001 stays byte-identical to the vendored copy, so migration 0004 sets the
   default itself.
2. The real-Postgres proof skipped on any connection error, even when a DSN or password
   was configured, so a configured database that failed read as a skip. Only an unset
   configuration may skip.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from tests.test_omn21288_skill_lifecycle_real_postgres import _connect_or_skip

pytestmark = pytest.mark.unit

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle/migrations"
    / "0004_set_received_at_default_skill_executions.sql"
)
_ENV = (
    "INTEGRATION_POSTGRES_DSN",
    "INTEGRATION_POSTGRES_PASSWORD",
    "POSTGRES_PASSWORD",
)


def test_migration_sets_the_received_at_default_on_an_existing_column() -> None:
    sql = _MIGRATION.read_text()
    assert re.search(
        r"^ALTER TABLE public\.skill_executions ALTER COLUMN received_at "
        r"SET DEFAULT NOW\(\);$",
        sql,
        re.IGNORECASE | re.MULTILINE,
    ), "no ALTER COLUMN received_at SET DEFAULT NOW() in migration 0004"


def _outcome_of_connect(monkeypatch: pytest.MonkeyPatch, **env: str) -> str:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    try:
        asyncio.run(_connect_or_skip())
    except pytest.skip.Exception:
        return "skipped"
    except pytest.fail.Exception:
        return "failed"
    return "connected"


def test_a_configured_dsn_that_cannot_connect_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome_of_connect(
        monkeypatch, INTEGRATION_POSTGRES_DSN="postgresql://u@127.0.0.1:1/none"
    )
    assert outcome == "failed"


def test_a_configured_password_that_cannot_connect_fails_instead_of_skipping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _outcome_of_connect(
        monkeypatch,
        INTEGRATION_POSTGRES_PASSWORD="x",
        INTEGRATION_POSTGRES_HOST="127.0.0.1",
        INTEGRATION_POSTGRES_PORT="1",
    )
    assert outcome == "failed"


def test_nothing_configured_still_skips(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _outcome_of_connect(monkeypatch) == "skipped"
