# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab_proof_receipts effect-class writer (OMN-19566, T2 slice 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_lab_proof_receipts.handlers.handler_lab_proof_receipts_writer import (
    _SELECT_PRIOR,
    _UPSERT,
    LabProofReceiptsProjectionWriter,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "lab_proof_receipts"
    / "omnibase_infra-4217-event.json"
)


def _producer_event() -> dict[str, Any]:
    loaded = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


class _Db:
    def __init__(self, prior: list[dict[str, Any]] | None = None) -> None:
        self.prior = prior or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.calls.append((query, params))
        if query == _SELECT_PRIOR:
            return self.prior
        if query == _UPSERT:
            return [
                {
                    "receipt_key": params[5],
                    "result": params[7],
                    "verifier_token": params[8],
                    "finished_at": params[14],
                    "projection_cursor": 1,
                }
            ]
        raise AssertionError(f"unexpected query: {query}")


@pytest.fixture(autouse=True)
def _db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIDASH_ANALYTICS_DB_URL", "postgresql://fixture/db")


def _writer(db: _Db) -> LabProofReceiptsProjectionWriter:
    writer = LabProofReceiptsProjectionWriter()
    writer._db = db  # type: ignore[assignment]

    async def _no_producer() -> None:
        return None

    writer._stop_producer = _no_producer  # type: ignore[method-assign]
    return writer


def test_the_producers_event_projects_verbatim() -> None:
    raw = _producer_event()
    db = _Db()
    out = _writer(db).handle({**raw, "_topic": raw["topic"]})
    assert out["rows_upserted"] == 1
    assert out["row"]["receipt_key"] == raw["receipt_key"]
    assert out["row"]["verifier_token"] == raw["verifier_token"]
    (select, _), (upsert, params) = db.calls
    assert select == _SELECT_PRIOR
    assert upsert == _UPSERT
    assert params[0:5] == (
        raw["repo"],
        raw["pr_number"],
        raw["head_sha"],
        raw["profile_id"],
        raw["profile_version"],
    )
    # the JSON columns go over the wire as JSON text for the ::jsonb casts
    assert json.loads(params[10]) == raw["mandatory_checks"]
    assert json.loads(params[20]) == raw["receipt"]


def test_the_writer_upserts_under_the_finished_at_guard() -> None:
    assert "ON CONFLICT (repo, pr_number, head_sha, profile_id, profile_version)" in (
        _UPSERT
    )
    assert (
        "WHERE omninode_internal.lab_proof_receipts.finished_at < EXCLUDED.finished_at"
        in _UPSERT
    )


def test_a_stored_newer_proof_means_no_upsert_at_all() -> None:
    raw = _producer_event()
    stored = {
        **{
            k: raw[k]
            for k in raw
            if k not in {"schema_version", "event_type", "topic", "lane"}
        },
        "finished_at": datetime(2999, 1, 1, tzinfo=UTC),
        "mandatory_checks": json.dumps(raw["mandatory_checks"]),
        "missing_mandatory_checks": json.dumps(raw["missing_mandatory_checks"]),
        "failing_checks": json.dumps(raw["failing_checks"]),
        "receipt": json.dumps(raw["receipt"]),
    }
    db = _Db(prior=[stored])
    out = _writer(db).handle(dict(raw))
    assert out == {"rows_upserted": 0, "row": None}
    assert [query for query, _ in db.calls] == [_SELECT_PRIOR]


def test_injected_keys_are_stripped_but_a_stray_field_dead_letters() -> None:
    raw = _producer_event()
    injected = {**raw, "_db": object(), "_event_type": "x", "_envelope_id": "e"}
    assert _writer(_Db()).handle(injected)["rows_upserted"] == 1
    with pytest.raises(ValidationError):
        _writer(_Db()).handle({**raw, "surprise": 1})


def test_only_the_writer_is_routed_and_it_dispatches_in_process() -> None:
    import yaml

    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_projection_lab_proof_receipts/contract.yaml"
        ).read_text(encoding="utf-8")
    )
    routed = [e["handler"]["name"] for e in contract["handler_routing"]["handlers"]]
    assert routed == ["LabProofReceiptsProjectionWriter"]
    assert LabProofReceiptsProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert (
        LabProofReceiptsProjectionWriter().subscribe_topics
        == [
            "onex.evt.omnibase-infra.lab-proof-receipt.v1"  # onex-topic-allow: asserting the contract's declared input
        ]
    )
