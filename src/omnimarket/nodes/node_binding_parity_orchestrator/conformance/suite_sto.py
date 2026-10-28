# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State store conformance (STO), including persistence and integer head selection."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from omnibase_core.protocols.storage.protocol_state_store import ProtocolStateStore

from omnimarket.nodes.node_binding_parity_orchestrator.conformance.suite_core import (
    Check,
    ModelCheckResult,
    expect,
    run_checks,
)
from omnimarket.nodes.node_binding_parity_orchestrator.stubs.stub_state import (
    StateHarness,
)

type HeadReader = Callable[[ProtocolStateStore, str], Awaitable[int]]


def _envelope(
    node_id: str = "sample", scope_id: str = "default", version: int = 1
) -> ModelStateEnvelope:
    return ModelStateEnvelope(
        node_id=node_id,
        scope_id=scope_id,
        data={"version": version, "nested": {"values": [1, "two", None]}},
        written_at=datetime(2026, 1, 1, tzinfo=UTC),
        contract_fingerprint="sto-conformance-v1",
    )


async def _check_roundtrip(harness: StateHarness) -> None:
    envelope = _envelope()
    await harness.store.put(envelope)
    expect(
        await harness.store.get(envelope.node_id) == envelope,
        "put/get changed the envelope",
    )


async def _check_overwrite(harness: StateHarness) -> None:
    await harness.store.put(_envelope(version=1))
    latest = _envelope(version=2)
    await harness.store.put(latest)
    expect(
        await harness.store.get(latest.node_id) == latest,
        "get returned an unlabelled stale write",
    )


async def _check_missing(harness: StateHarness) -> None:
    expect(
        await harness.store.get("missing") is None, "missing key did not return None"
    )


async def _check_exists(harness: StateHarness) -> None:
    store = harness.store
    expect(not await store.exists("sample"), "missing key exists")
    await store.put(_envelope())
    expect(await store.exists("sample"), "written key does not exist")
    expect(not await store.exists("sample", "other"), "scope isolation failed")
    await store.delete("sample")
    expect(not await store.exists("sample"), "deleted key still exists")


async def _check_delete(harness: StateHarness) -> None:
    store = harness.store
    expect(not await store.delete("sample"), "delete reported a missing key existed")
    await store.put(_envelope())
    expect(not await store.delete("sample", "other"), "delete crossed scopes")
    expect(await store.delete("sample"), "delete did not report an existing key")
    expect(await store.get("sample") is None, "delete retained the envelope")
    expect(not await store.delete("sample"), "repeated delete reported an existing key")


async def _check_list_keys(harness: StateHarness) -> None:
    store = harness.store
    expect(await store.list_keys() == [], "fresh store contains keys")
    keys = {("alpha", "2"), ("alpha", "10"), ("beta", "2")}
    for node, scope in sorted(keys):
        await store.put(_envelope(node, scope))
    expect(set(await store.list_keys()) == keys, "unfiltered keys are incomplete")
    expect(
        set(await store.list_keys("alpha"))
        == {key for key in keys if key[0] == "alpha"},
        "node filter returned the wrong keys",
    )
    expect(await store.list_keys("missing") == [], "missing node has keys")
    await store.delete("alpha", "2")
    expect(
        set(await store.list_keys()) == keys - {("alpha", "2")},
        "deleted key remains listed",
    )


async def _check_restart(harness: StateHarness) -> None:
    envelopes = (_envelope("sample", "2"), _envelope("sample", "10", 2))
    for envelope in envelopes:
        await harness.store.put(envelope)
    restarted = await harness.restart()
    for envelope in envelopes:
        expect(
            await restarted.get(envelope.node_id, envelope.scope_id) == envelope,
            "restart lost a persisted write",
        )


async def highest_witness_sequence(store: ProtocolStateStore, node_id: str) -> int:
    """Select the highest sequence numerically regardless of key enumeration order."""
    return max(int(scope_id) for _, scope_id in await store.list_keys(node_id))


async def highest_witness_sequence_as_text(
    store: ProtocolStateStore, node_id: str
) -> int:
    """Mutation reader that mistakenly ranks decimal sequence text lexicographically."""
    return int(max(scope_id for _, scope_id in await store.list_keys(node_id)))


async def _check_head(
    harness: StateHarness, head_reader: HeadReader = highest_witness_sequence
) -> None:
    for sequence in (7, 2, 12, 1, 9, 4, 10, 6, 3, 11, 5, 8):
        await harness.store.put(_envelope("head_witness", str(sequence), sequence))
    expect(
        await head_reader(harness.store, "head_witness") == 12,
        "head witness must select integer sequence 12",
    )


STO_CHECKS: tuple[Check[StateHarness], ...] = (
    ("put_then_get_roundtrip", _check_roundtrip),
    ("overwrite_reads_latest_write", _check_overwrite),
    ("get_missing_returns_none", _check_missing),
    ("exists_tracks_put_and_delete", _check_exists),
    ("delete_reports_whether_a_key_existed", _check_delete),
    ("list_keys_complete_and_filtered", _check_list_keys),
    ("data_survives_restart", _check_restart),
    ("head_witness_highest_sequence_is_integer", _check_head),
)


async def run_sto_suite(
    make_harness: Callable[[str], StateHarness],
    head_reader: HeadReader = highest_witness_sequence,
) -> tuple[ModelCheckResult, ...]:
    """Run each persistence check against a fresh harness with the selected head reader."""

    async def check_head(harness: StateHarness) -> None:
        await _check_head(harness, head_reader)

    checks = (*STO_CHECKS[:-1], (STO_CHECKS[-1][0], check_head))
    return await run_checks(checks, make_harness)
