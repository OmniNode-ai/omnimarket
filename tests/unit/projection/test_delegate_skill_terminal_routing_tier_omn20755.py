# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegate-skill terminal stores the tier it was routed to (OMN-20755).

``onex delegate``'s receipt names its ``routing_tier``, and each attempt on the
terminal names its ``tier``, but the delegate-skill row builder never named
``cost_tier_name``. On the OMN-20223 walk's install all 23 rows stored NULL
while every receipt said ``cheap_frontier``, so the Overview's Tier mix (the
model-routing view, OMN-20754) showed every run as not tier-routed.

Each test names the failure it exists to catch:

* the accepting attempt's tier is not stored (AC1);
* an escalated run stores the refused first rung's tier instead of the rung
  that answered;
* a run refused on every rung stores nothing, though it was routed;
* a terminal with no attempts erases a tier an earlier terminal stored;
* the Tier mix still shows a tiered run as not tier-routed (AC2, served half).

Every store is a SQLite file under pytest's ``tmp_path``, written through the
same ``project_delegate_skill_terminal`` call ``onex delegate`` makes, and read
back through the real read node.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_TENANT = "bc08ff01-9d50-442c-aca1-7ad2e687db70"
_ROUTING = "onex.snapshot.projection.delegation.model-routing.v1"
_CORRELATION = "f3667fd0-4475-4acd-bdea-623f5c0f9168"

# node_projection_delegation's own exposures, parsed by the topic map's section
# parser: discovering every contract takes seconds, and dod-verify gives this
# file 30.
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
)
_TOPICS = {
    cfg.topic: cfg
    for cfg in load_projection_exposures_from_contract(
        yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
        "node_projection_delegation",
        _CONTRACT,
    )
}


class _NoRepublish:
    """The projection's snapshot republish is not under test here."""

    def publish(self, *_args: object, **_kwargs: object) -> None:
        return None


def _attempt(
    tier: str, *, passed: bool = True, failure: str | None = None
) -> dict[str, Any]:
    return {
        "tier": tier,
        "backend_id": f"{tier}-backend",
        "model_id": "gemini-3.5-flash-lite",
        "quality_gate_passed": passed,
        "failure_class": failure,
    }


def _terminal(
    attempts: list[dict[str, Any]], **fields: Any
) -> ModelDelegateSkillTerminalProjection:
    payload: dict[str, Any] = {
        "status": "completed",
        "correlation_id": _CORRELATION,
        "task_type": "reasoning",
        "tenant_id": _TENANT,
        "model_name": "gemini-3.5-flash-lite",
        "quality_gate_passed": True,
        "emitted_at": "2026-10-08T18:31:00+00:00",
        "attempts": attempts,
        **fields,
    }
    return ModelDelegateSkillTerminalProjection.from_payload(payload)


def _register_tenant(path: Path) -> None:
    """Record the tenant in the store's mirror, as ``onex local init`` does.

    The projection confirms a terminal's tenant against tenant_registry_mirror
    and refuses one nobody recorded (OMN-16831). This is the shape
    omnimarket.local_deployment.tenant_identity creates.
    """
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tenant_registry_mirror (tenant_slug TEXT NOT NULL "
            "UNIQUE, tenant_uuid TEXT NOT NULL, display_name TEXT, status TEXT NOT NULL, "
            "registry_created_at TEXT, observed_at TEXT NOT NULL, source_event_id TEXT)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO tenant_registry_mirror (tenant_slug, tenant_uuid, "
            "status, observed_at) VALUES ('walk-install', ?, 'active', "
            "'2026-10-08T18:09:00+00:00')",
            (_TENANT,),
        )


def _project(path: Path, terminal: ModelDelegateSkillTerminalProjection) -> None:
    _register_tenant(path)
    HandlerProjectionDelegation(
        publisher=_NoRepublish()
    ).project_delegate_skill_terminal(terminal, SqliteDatabaseAdapter(path))


def _stored_tier(path: Path) -> object:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT cost_tier_name FROM delegation_events WHERE correlation_id = ?",
            (_CORRELATION,),
        ).fetchone()
    assert row is not None
    return row[0]


def test_the_accepting_attempts_tier_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    _project(path, _terminal([_attempt("cheap_frontier")]))
    assert _stored_tier(path) == "cheap_frontier"


def test_an_escalated_run_stores_the_rung_that_answered(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    _project(
        path,
        _terminal(
            [
                _attempt("local", passed=False, failure="quality_gate_failed"),
                _attempt("cheap_frontier"),
            ]
        ),
    )
    assert _stored_tier(path) == "cheap_frontier"


def test_a_run_refused_on_every_rung_stores_the_last_rung_it_reached(
    tmp_path: Path,
) -> None:
    path = tmp_path / "delegation.sqlite"
    _project(
        path,
        _terminal(
            [
                _attempt("local", passed=False, failure="quality_gate_failed"),
                _attempt("cheap_frontier", passed=False, failure="provider_quota"),
            ],
            status="failed",
            quality_gate_passed=False,
        ),
    )
    assert _stored_tier(path) == "cheap_frontier"


def test_a_terminal_with_no_attempts_leaves_a_stored_tier_alone(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    _project(path, _terminal([_attempt("cheap_frontier")]))
    _project(path, _terminal([]))
    assert _stored_tier(path) == "cheap_frontier"


def test_a_terminal_with_no_attempts_stores_no_tier(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    _project(path, _terminal([]))
    assert _stored_tier(path) is None


def test_the_tier_mix_lists_the_run_under_its_tier(tmp_path: Path) -> None:
    """AC2, served half: the model-routing row the Overview's Tier mix reads."""
    path = tmp_path / "delegation.sqlite"
    _project(path, _terminal([_attempt("cheap_frontier")]))
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_ROUTING, tenant_id=_TENANT))
    )
    assert result.http_status == 200, result.response
    by_tier = (result.response or {})["rows"][0]["by_tier"]
    assert by_tier["tier_routed_total"] == 1
    assert by_tier["not_tier_routed_count"] == 0
    assert by_tier["tiers"] == [
        {
            "cost_tier_name": "cheap_frontier",
            "count": 1,
            "tier_routed": True,
            "pct_of_tier_routed": 1.0,
        }
    ]
