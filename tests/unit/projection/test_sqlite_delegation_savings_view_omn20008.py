# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local store serves each run's basis on delegation.savings.v1 (OMN-20008).

The local dashboard's Basis column is ``sessions[].usage_source`` of
``delegation.savings.v1``, looked up by the run's correlation id. On Postgres
that exposure is the ``projection_delegation_savings`` view (migration 090). A
local store had no such relation, so every read answered 503
``projection_table_missing`` and a local run whose provider reported its tokens
read Basis "Not recorded", although the local writer had stored
``llm_call_metrics.usage_source = 'measured'`` for it. ``SqliteDatabaseAdapter``
now creates the view as one store step.

Each test names the failure it exists to catch:

* one recorded delegation still reads 503, or 200 with no row (AC2 falsifier:
  a run row whose basis is absent);
* a run's basis is not the stored provenance: a provider-measured run does not
  read measured, an estimated one does not read estimated;
* a run with token counts but no stored provenance reads measured, which would
  be a basis inferred from a default instead of read from the record;
* a run with several calls reads measured although one of them was not;
* the view serves another tenant's runs;
* a store created before the step never gains the view.

Every store is a SQLite file under pytest's ``tmp_path``. Reads go through the
real read node over :class:`SqliteTableRowSource`, as ``onex dashboard`` does.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_SAVINGS = "onex.snapshot.projection.delegation.savings.v1"
_TENANT = "a8a6827a-48b1-42e2-886c-41c4f0243946"
_OTHER = "11111111-2222-4333-8444-555555555555"

# The exposure as node_projection_savings' own contract declares it, parsed by
# the same section parser the topic map uses (see the OMN-20754 test for why
# this does not discover every contract).
_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_savings/contract.yaml"
)
_TOPICS = {
    cfg.topic: cfg
    for cfg in load_projection_exposures_from_contract(
        yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
        "node_projection_savings",
        _CONTRACT,
    )
}


def _delegation(correlation_id: str, **fields: Any) -> dict[str, Any]:
    # The shape the local delegate run of 2026-10-10 stored (run 967353cf,
    # Qwen3.8-27B, 213 tokens in, 6 out, $0 spent, $0.000486 saved).
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "document",
        "delegated_to": "Qwen3.8-27B",
        "model_name": "Qwen3.8-27B",
        "cost_tier_name": "local",
        "cost_usd": 0.0,
        "cost_savings_usd": 0.000486,
        "tokens_input": 213,
        "tokens_output": 6,
        "pricing_manifest_version": 1,
        "latency_ms": 321,
        "data_source": "real",
        "created_at": "2026-10-10T18:13:00.608068+00:00",
    }
    row.update(fields)
    return row


def _call(correlation_id: str, usage_source: str, call: int = 0) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "session_id": correlation_id,
        "model_id": "Qwen3.8-27B",
        "prompt_tokens": 213,
        "completion_tokens": 6,
        "total_tokens": 219,
        "usage_source": usage_source,
        "usage_is_estimated": usage_source == "estimated",
        "input_hash": f"{correlation_id}-{call}",
        "created_at": "2026-10-10T18:13:00.623992+00:00",
    }


def _store(
    tmp_path: Path,
    runs: tuple[dict[str, Any], ...],
    calls: tuple[dict[str, Any], ...] = (),
) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in runs:
        adapter.upsert("delegation_events", "correlation_id", row)
    for call in calls:
        adapter.upsert("llm_call_metrics", "input_hash", call)
    return path


def _read(path: Path, tenant: str = _TENANT) -> tuple[int, dict[str, Any]]:
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_SAVINGS, tenant_id=tenant))
    )
    return result.http_status, dict(result.response or {})


def _row(path: Path, tenant: str = _TENANT) -> dict[str, Any]:
    status, body = _read(path, tenant)
    assert status == 200, body
    assert body["row_count"] == 1, body
    row: dict[str, Any] = body["rows"][0]
    return row


def _basis(path: Path, tenant: str = _TENANT) -> dict[str, str]:
    return {
        str(session["session_id"]): str(session["usage_source"])
        for session in _row(path, tenant)["sessions"]
    }


def test_a_provider_measured_local_run_reads_measured(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        (_delegation("4016510b-3e75-471d-8e6f-e12cf5f6cb09"),),
        (_call("4016510b-3e75-471d-8e6f-e12cf5f6cb09", "measured"),),
    )
    row = _row(path)
    assert row["tenant_id"] == _TENANT
    assert row["session_count"] == 1
    [session] = row["sessions"]
    assert session["session_id"] == "4016510b-3e75-471d-8e6f-e12cf5f6cb09"
    assert session["usage_source"] == "measured"
    assert session["savings_method"] == "measured"
    assert session["local_cost_usd"] == 0.0
    assert session["savings_usd"] == pytest.approx(0.000486)
    assert session["counterfactual_baseline_usd"] == pytest.approx(0.000486)
    assert (session["prompt_tokens"], session["completion_tokens"]) == (213, 6)
    assert row["cumulative_savings_usd"] == pytest.approx(0.000486)
    assert row["cumulative_local_cost_usd"] == 0.0


def test_the_basis_is_the_stored_provenance_never_a_token_default(
    tmp_path: Path,
) -> None:
    path = _store(
        tmp_path,
        (_delegation("m"), _delegation("e"), _delegation("u"), _delegation("none")),
        (_call("m", "measured"), _call("e", "estimated"), _call("u", "unknown")),
    )
    # "none" carries 213/6 tokens like the others but no stored provenance: it
    # reads the contract's refusal value, not a basis inferred from its tokens.
    assert _basis(path) == {
        "m": "measured",
        "e": "estimated",
        "u": "unknown",
        "none": "unknown",
    }
    methods = {s["session_id"]: s["savings_method"] for s in _row(path)["sessions"]}
    assert methods == {
        "m": "measured",
        "e": "estimated",
        "u": "estimated",
        "none": "estimated",
    }


def test_a_run_is_measured_only_when_every_call_was(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        (_delegation("all"), _delegation("mix"), _delegation("gap")),
        (
            _call("all", "measured", 0),
            _call("all", "measured", 1),
            _call("mix", "measured", 0),
            _call("mix", "estimated", 1),
            _call("gap", "measured", 0),
            _call("gap", "unknown", 1),
        ),
    )
    assert _basis(path) == {"all": "measured", "mix": "estimated", "gap": "unknown"}


def test_each_tenant_reads_only_its_own_runs(tmp_path: Path) -> None:
    path = _store(
        tmp_path,
        (_delegation("a"), _delegation("b"), _delegation("c", tenant_id=_OTHER)),
        (_call("a", "measured"), _call("c", "estimated")),
    )
    assert _basis(path) == {"a": "measured", "b": "unknown"}
    assert _basis(path, _OTHER) == {"c": "estimated"}


def test_a_store_created_before_the_step_gains_the_view_on_its_next_open(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, (_delegation("old"),), (_call("old", "measured"),))
    conn = sqlite3.connect(path)
    conn.execute("DROP VIEW projection_delegation_savings")
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn20008_delegation_savings_view'"
    )
    conn.commit()
    conn.close()
    assert _read(path)[0] == 503

    SqliteDatabaseAdapter(path)._connect().close()

    assert _basis(path) == {"old": "measured"}
