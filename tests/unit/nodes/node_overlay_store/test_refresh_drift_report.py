# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A loader run reports each setting whose repository value differs from its live operator value (OMN-26495).

Golden chain and error chain under one correlation id: the drift report and the loaded record of the golden run,
the refusal of the run whose live record the store cannot read.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store import ModelOverlayStoreLoadOverlay
from omnimarket.nodes.node_overlay_store_load_effect.handlers import (
    HandlerOverlayStoreLoad,
    OverlayHomeSource,
    OverlayLoadRefusedError,
)
from omnimarket.nodes.node_overlay_store_load_effect.runtime_overlay_store_load import (
    _run,
)
from tests.unit.nodes.node_overlay_store.support import (
    LOADED_TOPIC,
    NOW,
    FakeBus,
    Store,
    declared_topic,
    overlay_home,
    record,
    table_with,
)

pytestmark = pytest.mark.unit

USER = "hosts/[name=h1]/ssh_user"
REPO_USER = "repo-user-xyz"
OPERATOR_USER = "operator-user-xyz"
CORRELATION = "11111111-2222-3333-4444-555555555555"


def value_digest(value: Any) -> str:
    """The digest a drift report carries in place of a value: sha256 of the value's canonical JSON."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _handler(
    tmp_path: Path, bus: FakeBus, live: Any, repository: dict[str, Any]
) -> HandlerOverlayStoreLoad:
    return HandlerOverlayStoreLoad(
        source=OverlayHomeSource(overlay_home(tmp_path, repository)),
        bus=bus,
        topic=LOADED_TOPIC,
        loaded_by="test",
        clock=lambda: NOW,
        live=live,
        drift_topic=declared_topic("overlay-store-drift"),
        refusal_topic=declared_topic("overlay-store-refused"),
    )


async def _live(*, user: str, paths: tuple[str, ...]) -> InMemoryOverlayStore:
    store = InMemoryOverlayStore()
    assert await store.write_version(
        record(table_with(ssh_user=user), operator_paths=paths).model_dump(mode="json")
    )
    return store


def _on(bus: FakeBus, topic: str) -> list[dict[str, Any]]:
    return [json.loads(value) for t, _, value in bus.published if t == topic]


def test_topic_declared_by_the_loader_contract() -> None:
    drift = declared_topic("overlay-store-drift")
    refused = declared_topic("overlay-store-refused")
    assert drift != refused
    assert LOADED_TOPIC not in (drift, refused)


@pytest.mark.asyncio
async def test_drift_reported_once_per_differing_setting_with_digests_and_no_value(
    tmp_path: Path,
) -> None:
    bus = FakeBus()
    live = await _live(user=OPERATOR_USER, paths=(USER, "hosts/[name=h1]/cap"))
    repository = table_with(ssh_user=REPO_USER)  # cap equal, ssh_user different
    result = await _handler(tmp_path, bus, live, repository).handle(
        "lab_hosts", correlation_id=CORRELATION
    )
    drift_topic = declared_topic("overlay-store-drift")
    events = _on(bus, drift_topic)
    assert len(events) == 1
    event = events[0]
    assert event["overlay_id"] == "lab_hosts"
    assert event["setting_path"] == USER
    assert event["repository_digest"] == value_digest(REPO_USER)
    assert event["live_digest"] == value_digest(OPERATOR_USER)
    assert event["correlation_id"] == CORRELATION
    payload = next(v for t, _, v in bus.published if t == drift_topic)
    assert REPO_USER.encode() not in payload
    assert OPERATOR_USER.encode() not in payload
    assert result.drifted_paths == (USER,)


@pytest.mark.asyncio
async def test_no_drift_when_equal_publishes_no_drift_report(tmp_path: Path) -> None:
    bus = FakeBus()
    live = await _live(user=REPO_USER, paths=(USER,))
    result = await _handler(tmp_path, bus, live, table_with(ssh_user=REPO_USER)).handle(
        "lab_hosts"
    )
    assert _on(bus, declared_topic("overlay-store-drift")) == []
    assert result.drifted_paths == ()
    assert result.operator_paths == (USER,)  # still an operator-set value until cleared


@pytest.mark.asyncio
async def test_golden_chain_and_error_chain_carry_one_correlation_id(
    tmp_path: Path,
) -> None:
    # Golden: the loaded record, a drift report and the result.
    (tmp_path / "error").mkdir()
    bus = FakeBus()
    live = await _live(user=OPERATOR_USER, paths=(USER,))
    repository = table_with(ssh_user=REPO_USER)
    golden = await _handler(tmp_path, bus, live, repository).handle(
        "lab_hosts", correlation_id=CORRELATION
    )
    assert golden.correlation_id == CORRELATION
    assert [t for t, _, _ in bus.published] == [
        LOADED_TOPIC,
        declared_topic("overlay-store-drift"),
    ]
    assert (
        _on(bus, declared_topic("overlay-store-drift"))[0]["correlation_id"]
        == CORRELATION
    )
    # Error: the live record cannot be read; one refusal on the refused topic, nothing loaded.
    failing = FakeBus()
    with pytest.raises(OverlayLoadRefusedError):
        await _handler(
            tmp_path / "error", failing, Store(down=True), repository
        ).handle("lab_hosts", correlation_id=CORRELATION)
    refused = _on(failing, declared_topic("overlay-store-refused"))
    assert len(refused) == 1
    assert refused[0]["correlation_id"] == CORRELATION
    assert refused[0]["overlay_id"] == "lab_hosts"
    assert [t for t, _, _ in failing.published] == [
        declared_topic("overlay-store-refused")
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("breaks", ["store_down", "record_malformed"])
async def test_unreadable_live_record_is_refused_and_never_replaced(
    tmp_path: Path, breaks: str
) -> None:
    bus = FakeBus()
    if breaks == "store_down":
        live: Any = Store(down=True)
    else:
        live = Store(
            {
                "overlay_id": "lab_hosts",
                "digest": "x",
                "document": {"secret": OPERATOR_USER},
            }
        )
    with pytest.raises(OverlayLoadRefusedError) as raised:
        await _handler(tmp_path, bus, live, table_with(ssh_user=REPO_USER)).handle(
            "lab_hosts", correlation_id=CORRELATION
        )
    assert LOADED_TOPIC not in [t for t, _, _ in bus.published]
    assert OPERATOR_USER not in str(raised.value)
    refusal = _on(bus, declared_topic("overlay-store-refused"))
    assert len(refusal) == 1
    assert OPERATOR_USER not in json.dumps(refusal)


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [None, 0.05])
async def test_runtime_run_names_one_correlation_id_and_reads_live_only_where_told(
    tmp_path: Path, window: float | None
) -> None:
    overlay = ModelOverlayStoreLoadOverlay(
        bus="inmemory", loaded_by="test", live_read_window_s=window
    )
    clone = overlay_home(tmp_path, table_with(ssh_user=REPO_USER))
    results = await _run(overlay, clone, ["lab_hosts"], None)
    assert len(results) == 1
    assert results[0].correlation_id
    assert results[0].operator_paths == ()
    assert results[0].drifted_paths == ()
