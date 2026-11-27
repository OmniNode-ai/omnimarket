# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each check receipt names where its responder chain came from (OMN-26720).

A reader of ``last-run.json`` must tell a chain loaded from the settings store from one read from an overlay: the
receipt carries the source, its source_ref, its loaded_at stamp and the declared rows. The rows are read through the
injected store reader, never a file path.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from _gateway_fake import FakeGateway
from omnibase_core.types import JsonType

from omnimarket.handlers.overlay_store.reader_store import StoreOverlayReader
from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.models.overlay_store.model_overlay_store import (
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    contract_topics,
)

LOADED_AT = datetime(2026, 11, 26, 21, 37, 29, tzinfo=UTC)
SOURCE_REF = "settings-repo@abc123"
DOCUMENT: dict[str, JsonType] = {
    "schema_version": 1,
    "responders": [{"tier": "openrouter"}],
}


def _probe_overlay(tmp: Path) -> str:
    probe = tmp / "probe.yaml"
    probe.write_text(
        yaml.safe_dump(
            {
                "checks": ["C13", "C14", "C17"],
                "history_file": "runs.jsonl",
                "history_keep": 5,
                "result_topic": contract_topics()[0],
                "responder_timeout_s": 1,
            }
        )
    )
    return str(probe)


def _store_with_row() -> InMemoryOverlayStore:
    store = InMemoryOverlayStore()
    record = ModelOverlayStoreRecord(
        overlay_id="delegation_check_responders",
        digest=document_digest(DOCUMENT),  # type: ignore[arg-type]
        document=DOCUMENT,  # type: ignore[arg-type]
        source_ref=SOURCE_REF,
        loaded_by="test",
        loaded_at=LOADED_AT,
    )
    asyncio.run(store.write_version(record.model_dump(mode="json")))
    return store


def _run(tmp: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> list[dict]:
    monkeypatch.setenv(tick.OVERLAY_FILE_ENV, _probe_overlay(tmp))
    tick.main(
        ["--omni-home", str(tmp), "--state-dir", str(tmp / "state")],
        publisher=InMemoryCheckPublisher(),
        gateway=FakeGateway(),
        **kwargs,  # type: ignore[arg-type]
    )
    return json.loads((tmp / "state" / tick.EVIDENCE_FILE).read_text())["runs"]


def test_every_receipt_names_the_store_its_ref_stamp_and_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = _run(
        tmp_path,
        monkeypatch,
        store_reader=StoreOverlayReader(_store_with_row()),
    )
    assert [r["check"] for r in runs] == ["C13", "C14", "C17"]
    for run in runs:
        source = run["chain_source"]
        assert source["source"] == "store"
        assert source["source_ref"] == SOURCE_REF
        assert source["loaded_at"] == "2026-11-26T21:37:29Z"
        assert source["rows"] == [{"tier": "openrouter", "base_url": None}]


def test_a_store_reader_wins_over_a_pinned_overlay_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "responders.yaml"
    other.write_text(
        yaml.safe_dump({"schema_version": 1, "responders": [{"tier": "claude_api"}]})
    )
    monkeypatch.setenv(tick.RESPONDERS_FILE_ENV, str(other))
    runs = _run(
        tmp_path,
        monkeypatch,
        store_reader=StoreOverlayReader(_store_with_row()),
    )
    assert runs[0]["chain_source"]["rows"] == [{"tier": "openrouter", "base_url": None}]


def test_an_overlay_chain_is_named_overlay_not_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = tmp_path / "responders.yaml"
    other.write_text(
        yaml.safe_dump({"schema_version": 1, "responders": [{"tier": "claude_api"}]})
    )
    monkeypatch.setenv(tick.RESPONDERS_FILE_ENV, str(other))
    runs = _run(tmp_path, monkeypatch)
    for run in runs:
        assert run["chain_source"]["source"] == "overlay"
        assert run["chain_source"]["loaded_at"] is None
        assert str(other) not in json.dumps(run)
        assert run["chain_source"]["rows"] == [{"tier": "claude_api", "base_url": None}]


def test_no_row_at_all_leaves_the_source_unnamed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(tick.RESPONDERS_FILE_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    runs = _run(tmp_path, monkeypatch)
    assert all(r["chain_source"] is None for r in runs)
