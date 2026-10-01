# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19841: a ranked exposure's page is its top rows, not its oldest cursors.

Measured on the .201 dev lane on 2026-09-27: the runtime-error fingerprint
exposure (``occurrence_count DESC, last_seen_at DESC, projection_cursor DESC``,
limit 200) served cursors 134..3909 while its table reached 17545, and a runtime
error produced as a positive control never reached the Errors panel, reload or
not. ``projection_query`` cut every page in ascending cursor order (the
OMN-18043 walk) and ranked only the cut, so while the serving cache retained
more than one page the page was the OLDEST ``limit`` rows.

Every test drives the REAL ``SnapshotCache`` through ``apply_message`` and the
REAL fingerprint contract exposure, so the served window is what the live
route computes, not a mock's echo.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

import omnimarket.nodes.node_projection_runtime_error_fingerprints as fingerprint_node
from omnimarket.projection.api_server import app, get_row_source, get_topic_map
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.snapshot_cache import SnapshotCache
from tests.helpers.cache_row_source import CacheRowSource

pytestmark = pytest.mark.unit

_CONTRACT_PATH = Path(fingerprint_node.__file__).parent / "contract.yaml"
_SOURCE_TOPIC = "onex.evt.omnibase-infra.runtime-error.v1"
# More rows than the contract limit (200), fewer than the cache's retention
# cap (limit * 4), so eviction plays no part in what is served.
_CACHED_ROWS = 250
# Two loud, old fingerprints. Everything else occurred once, newer as the
# cursor rises -- the lab's shape, where 1550 of 1730 rows sat at count 1.
_LOUD = {10: 50, 20: 50}


def _contract_exposure() -> ProjectionTableConfig:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), _CONTRACT_PATH
    )
    assert len(exposures) == 1
    return exposures[0]


def _row(cursor: int) -> dict[str, Any]:
    return {
        "fingerprint": f"fp-{cursor:04d}",
        "logger_name": "omnibase_infra.runtime.auto_wiring.handler_wiring",
        "error_category": "runtime",
        "category_evidence": "logger_prefix",
        "severity": "error",
        "message_template": f"error class {cursor}",
        "exception_type": "ValidationError",
        "occurrence_count": _LOUD.get(cursor, 1),
        "correlation_id": f"corr-{cursor}",
        "service_name": "omnibase_infra",
        "hostname": "omninode-runtime",
        "first_seen_at": "2026-09-26T18:00:00+00:00",
        # Seconds rise with the cursor: a higher cursor is a newer sighting.
        "last_seen_at": f"2026-09-26T{18 + cursor // 3600:02d}:"
        f"{(cursor % 3600) // 60:02d}:{cursor % 60:02d}+00:00",
        "projection_cursor": cursor,
    }


def _seeded_cache(cfg: ProjectionTableConfig) -> SnapshotCache:
    cache = SnapshotCache(
        {cfg.topic: cfg},
        bootstrap_servers="unused:9092",
        group_id="test-omn19841-ranked-page-selection",
    )
    # Applied newest-first so an ascending answer is the route's choice, not
    # the dict's insertion order echoed back.
    for offset, cursor in enumerate(range(_CACHED_ROWS, 0, -1), start=1):
        row = _row(cursor)
        payload = {
            "topic": cfg.topic,
            "key": [row["fingerprint"]],
            "op": "upsert",
            "row": row,
            "observed_at": row["last_seen_at"],
            "source_event_id": f"evt-{cursor}",
            "source_topic": _SOURCE_TOPIC,
            "source_partition": 0,
            "source_offset": offset,
            "projection_version": "projection_snapshot.v1",
        }
        cache.apply_message(
            cfg.topic,
            key=row["fingerprint"].encode("utf-8"),
            value=json.dumps(payload).encode("utf-8"),
            headers=[("tenant_id", b"omninode")],
        )
    cache._state[cfg.topic].bootstrap_complete = True
    assert cache.row_count(cfg.topic) == _CACHED_ROWS
    return cache


@contextmanager
def _client(
    cfg: ProjectionTableConfig, cache: SnapshotCache
) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_row_source] = lambda: CacheRowSource(cache)
    app.dependency_overrides[get_topic_map] = lambda: {cfg.topic: cfg}
    try:
        yield TestClient(app, raise_server_exceptions=True)
    finally:
        app.dependency_overrides.clear()


def _expected_top(limit: int) -> list[int]:
    """The declared ranking, computed independently of the route."""
    rows = [_row(cursor) for cursor in range(1, _CACHED_ROWS + 1)]
    rows.sort(
        key=lambda r: (
            -r["occurrence_count"],
            # ISO strings of one fixed width and offset order as instants.
            [-ord(ch) for ch in r["last_seen_at"]],
            -r["projection_cursor"],
        )
    )
    return [r["projection_cursor"] for r in rows[:limit]]


def _cursors(body: dict[str, Any]) -> list[int]:
    return [int(row["projection_cursor"]) for row in body["rows"]]


def test_page_selection_contract_declares_the_ranked_window() -> None:
    """AC2: the fingerprint exposure is a ranked read model and says so."""
    cfg = _contract_exposure()
    assert cfg.page_selection == "order_by"
    assert cfg.cursor_column == "projection_cursor"
    assert cfg.limit == 200


def test_page_selection_ranked_window_serves_the_top_rows_by_declared_order() -> None:
    """AC1: the newest error is on the page; the oldest single one is not."""
    cfg = _contract_exposure()
    with _client(cfg, _seeded_cache(cfg)) as client:
        resp = client.get(f"/projection/{cfg.topic}")
    assert resp.status_code == 200
    body = resp.json()
    served = _cursors(body)
    assert served == _expected_top(cfg.limit)
    assert served[:2] == [20, 10], "the loudest fingerprints lead the page"
    assert _CACHED_ROWS in served, "the newest error must reach the panel"
    assert 1 not in served, "the oldest single occurrence is ranked out"
    assert body["page_selection"] == "order_by"


def test_page_selection_ranked_window_cursor_starts_a_walk_that_skips_nothing() -> None:
    """A ranked page cannot continue from its own last row (its rows are
    scattered in cursor space), so a truncated one advertises the ORIGIN of the
    ascending walk (OMN-20327): following it reaches every key, and a caller
    that stops at the ranked page sees ``truncated``."""
    cfg = _contract_exposure()
    walked: set[int] = set()
    with _client(cfg, _seeded_cache(cfg)) as client:
        body = client.get(f"/projection/{cfg.topic}").json()
        assert body["truncated"] is True
        assert body["row_count"] == cfg.limit
        assert body["next_cursor"] == "0"
        walked.update(_cursors(body))
        since = body["next_cursor"]
        for _ in range(_CACHED_ROWS):
            body = client.get(
                f"/projection/{cfg.topic}", params={"since": since}
            ).json()
            walked.update(_cursors(body))
            if body["next_cursor"] is None:
                break
            since = body["next_cursor"]
    assert walked == set(range(1, _CACHED_ROWS + 1))


def test_page_selection_ranked_window_is_not_truncated_when_it_fits() -> None:
    cfg = _contract_exposure().model_copy(update={"limit": _CACHED_ROWS})
    with _client(cfg, _seeded_cache(cfg)) as client:
        body = client.get(f"/projection/{cfg.topic}").json()
    assert body["truncated"] is False
    assert body["next_cursor"] is None
    assert len(body["rows"]) == _CACHED_ROWS


def test_page_selection_since_still_walks_the_ascending_cursor() -> None:
    """The OMN-18043 walk is unchanged: ``since`` selects by cursor ASC."""
    cfg = _contract_exposure()
    walked: list[int] = []
    since = "0"
    with _client(cfg, _seeded_cache(cfg)) as client:
        for _ in range(_CACHED_ROWS):
            body = client.get(
                f"/projection/{cfg.topic}", params={"since": since}
            ).json()
            assert body["page_selection"] == "cursor"
            walked.extend(_cursors(body))
            if body["next_cursor"] is None:
                break
            since = body["next_cursor"]
    assert sorted(walked) == list(range(1, _CACHED_ROWS + 1))
    assert len(walked) == len(set(walked))


def test_page_selection_cursor_default_still_serves_the_lowest_cursor_window() -> None:
    """The default is untouched for every exposure that does not opt in -- and
    this is the exact window the fingerprint exposure served before."""
    cfg = _contract_exposure().model_copy(update={"page_selection": "cursor"})
    with _client(cfg, _seeded_cache(cfg)) as client:
        body = client.get(f"/projection/{cfg.topic}").json()
    assert sorted(_cursors(body)) == list(range(1, cfg.limit + 1))
    assert body["next_cursor"] == str(cfg.limit)
    assert body["page_selection"] == "cursor"


def test_page_selection_unknown_value_excludes_the_exposure() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    contract["projection_api"]["page_selection"] = "newest"
    assert (
        load_projection_exposures_from_contract(
            contract, str(contract["name"]), _CONTRACT_PATH
        )
        == ()
    )


def test_page_selection_order_by_without_a_declared_order_is_refused() -> None:
    with pytest.raises(ValueError, match="page_selection: order_by"):
        ProjectionTableConfig(
            topic="onex.snapshot.projection.test-omn19841.v1",
            table="t",
            columns=("projection_cursor",),
            cursor_column="projection_cursor",
            page_selection="order_by",
        )
