# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19787: acceptance-eval reads serve the latest run before older runs."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import product
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from fastapi.testclient import TestClient

import omnimarket.nodes.node_projection_delegation_eval as eval_node
from omnimarket.config.settings import get_settings
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache
from tests.helpers.cache_row_source import CacheRowSource

pytestmark = pytest.mark.unit

_CONTRACT_PATH = Path(eval_node.__file__).parent / "contract.yaml"
_TENANT_SLUG = "eval-house"
_TENANT_UUID = UUID("a1a1a1a1-0000-4000-8000-000000000001")
_TASK_CLASSES = (
    "code_generation",
    "code_review",
    "document",
    "planning",
    "reasoning",
    "research",
    "review",
    "summarization",
    "test",
)
_STRATA = ("all", "accepted")
_ARMS = ("recorded", "replayed", "recorded_judge")
_ROWS_PER_RUN = 54


@pytest.fixture(autouse=True)
def _no_lane_tenant(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("ONEX_TENANT_ID", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _contract_exposure() -> ProjectionTableConfig:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), _CONTRACT_PATH
    )
    assert len(exposures) == 1
    cfg = exposures[0]
    assert cfg.page_selection == "order_by"
    assert cfg.tenant_column == "tenant_id"
    return cfg


def _seeded_cache(cfg: ProjectionTableConfig) -> SnapshotCache:
    cache = SnapshotCache(
        {cfg.topic: cfg},
        bootstrap_servers="unused:9092",
        group_id="test-omn19787-acceptance-eval-latest-first",
    )
    rows: list[dict[str, Any]] = []
    combinations = product(range(1, 4), _TASK_CLASSES, _STRATA, _ARMS)
    for cursor, (run, task_class, stratum, arm) in enumerate(combinations, start=1):
        # Unused numeric columns are null; every declared column is present.
        row: dict[str, Any] = dict.fromkeys(cfg.columns)
        row.update(
            tenant_id=str(_TENANT_UUID),
            eval_run_id=f"run-{run}",
            task_class=task_class,
            stratum=stratum,
            arm=arm,
            manifest_id="manifest-eval",
            gate_version="gate.v1",
            rater_role="judge",
            rubric_version="rubric.v1",
            false_pass_line_verdict="undetermined",
            false_refusal_line_verdict="undetermined",
            observed_at=f"2026-10-0{run}T00:00:00+00:00",
            projection_cursor=cursor,
        )
        rows.append(row)
    # Newest-first insertion must not determine either ranking or cursor walks.
    for offset, row in enumerate(reversed(rows), start=1):
        key = [str(row[column]) for column in cfg.key_columns]
        payload = {
            "topic": cfg.topic,
            "key": key,
            "op": "upsert",
            "row": row,
            "observed_at": row["observed_at"],
            "source_event_id": f"evt-{row['projection_cursor']}",
            "source_topic": "onex.evt.omnimarket.delegation-eval-run-completed.v1",
            "source_partition": 0,
            "source_offset": offset,
            "projection_version": "projection_snapshot.v1",
        }
        cache.apply_message(
            cfg.topic,
            key="|".join(key).encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
            headers=[("tenant_id", str(_TENANT_UUID).encode("utf-8"))],
        )
    cache._state[cfg.topic].bootstrap_complete = True
    assert cache.row_count(cfg.topic) == 3 * _ROWS_PER_RUN
    return cache


@contextmanager
def _client(cfg: ProjectionTableConfig) -> Iterator[TestClient]:
    cache = _seeded_cache(cfg)
    app.dependency_overrides[get_row_source] = lambda: CacheRowSource(
        cache, tenant_registry={_TENANT_SLUG: _TENANT_UUID}
    )
    app.dependency_overrides[get_topic_map] = lambda: {cfg.topic: cfg}
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


def test_latest_run_every_class_on_first_page() -> None:
    cfg = _contract_exposure()
    with _client(cfg) as client:
        response = client.get(f"/projection/{cfg.topic}?tenant={_TENANT_SLUG}")
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["rows"]
    assert len(rows) == cfg.limit == 100
    assert rows[0]["eval_run_id"] == "run-3"
    newest = [row for row in rows if row["eval_run_id"] == "run-3"]
    assert len(newest) == _ROWS_PER_RUN
    assert {(row["task_class"], row["stratum"], row["arm"]) for row in newest} == set(
        product(_TASK_CLASSES, _STRATA, _ARMS)
    )


def test_since_still_walks_the_cursor() -> None:
    cfg = _contract_exposure()
    since = _ROWS_PER_RUN
    with _client(cfg) as client:
        response = client.get(
            f"/projection/{cfg.topic}?tenant={_TENANT_SLUG}&since={since}"
        )
    assert response.status_code == 200, response.text
    body = response.json()
    cursors = [int(row["projection_cursor"]) for row in body["rows"]]
    assert cursors
    assert all(cursor > since for cursor in cursors)
    assert sorted(cursors) == list(range(since + 1, since + cfg.limit + 1))
    assert body["page_selection"] == "cursor"
    assert body["next_cursor"] == str(since + cfg.limit)
