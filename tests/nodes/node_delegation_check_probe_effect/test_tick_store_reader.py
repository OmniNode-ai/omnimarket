# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The probe tick takes its settings from the injected store reader, not from a per-host path (OMN-26642).

A host whose unit does not name the settings snapshot root (measured on h051) found neither the gateway row nor the
responder row on disk and read PRECONDITION-ABSENT. The tick now asks the store reader for each settings document; with
no overlay file and no overlay root on the host, the reader's documents alone are enough.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from _gateway_fake import FakeGateway

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_delegation_check_probe_effect import (
    runtime_delegation_check_probe_tick as tick,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    InMemoryCheckPublisher,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    contract_topics,
)
from omnimarket.nodes.node_overlay_store_read_effect.handlers.handler_overlay_store_read import (
    OverlayStaleError,
)

pytestmark = pytest.mark.unit

GATEWAY_ROW = {
    "schema_version": 1,
    "gateway_url": "http://gateway.example.test:8080",
    "tenant_slug": "dev-tenant",
    "credential_kind": "api_key",
}


class StoreReader:
    def __init__(self, documents: dict[str, dict[str, Any]]) -> None:
        self.documents = documents
        self.asked: list[str] = []

    def __call__(self, overlay_id: str) -> dict[str, Any] | None:
        self.asked.append(overlay_id)
        return self.documents.get(overlay_id)


def _documents(checks: list[str]) -> dict[str, dict[str, Any]]:
    return {
        tick.NODE: {
            "checks": checks,
            "history_file": "runs.jsonl",
            "history_keep": 5,
            "result_topic": contract_topics()[0],
            "responder_timeout_s": 1,
        },
        tick.RESPONDERS_OVERLAY_ID: {
            "schema_version": 1,
            "responders": [{"tier": "openrouter"}],
        },
        "delegation_check_gateway": GATEWAY_ROW,
    }


def _bare_host(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        tick.OVERLAY_FILE_ENV,
        tick.RESPONDERS_FILE_ENV,
        "ONEX_DELEGATION_CHECK_GATEWAY_OVERLAY",
        "ONEX_SKILL_OVERLAY_ROOTS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_tick_runs_from_the_store_reader_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bare_host(monkeypatch)
    reader = StoreReader(_documents(["C17"]))
    pub = InMemoryCheckPublisher()
    tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=pub,
        gateway=FakeGateway(),
        store_reader=reader,
    )
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert "status" not in evidence, evidence
    assert [r["check"] for r in evidence["runs"]] == ["C17"]
    assert tick.NODE in reader.asked


def test_gateway_row_comes_from_the_store_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bare_host(monkeypatch)
    reader = StoreReader(_documents(["C13"]))
    built: list[Any] = []
    monkeypatch.setattr(
        tick,
        "HttpGatewayCaller",
        lambda row, *_, **__: built.append(row) or FakeGateway(),
    )
    tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=InMemoryCheckPublisher(),
        store_reader=reader,
    )
    assert [r.gateway_url for r in built] == ["http://gateway.example.test:8080"]
    assert "delegation_check_gateway" in reader.asked


def test_a_store_that_serves_no_gateway_row_reads_precondition_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bare_host(monkeypatch)
    documents = _documents(["C13"])
    del documents["delegation_check_gateway"]
    code = tick.main(
        ["--omni-home", str(tmp_path), "--state-dir", str(tmp_path / "state")],
        publisher=InMemoryCheckPublisher(),
        store_reader=StoreReader(documents),
    )
    evidence = json.loads((tmp_path / "state" / tick.EVIDENCE_FILE).read_text())
    assert code != 0
    assert evidence["runs"][0]["verdict"] == "PRECONDITION-ABSENT"


def test_an_overlay_the_store_does_not_keep_reads_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store keeps the two settings rows but not the probe's own overlay (measured on h051): that falls to the file chain."""

    def not_kept(overlay_id: str) -> object:
        raise NodeOverlayError(f"overlay {overlay_id}: this host keeps ['lab_hosts']")

    monkeypatch.setattr(tick, "read_overlay", not_kept)
    assert tick.read_store_document(tick.NODE) is None


def test_a_stale_refusal_is_not_read_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def stale(overlay_id: str) -> object:
        raise OverlayStaleError(f"overlay {overlay_id}: stale")

    monkeypatch.setattr(tick, "read_overlay", stale)
    with pytest.raises(OverlayStaleError):
        tick.read_store_document(tick.RESPONDERS_OVERLAY_ID)
