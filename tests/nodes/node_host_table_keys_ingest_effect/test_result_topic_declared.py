# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ingest node's result event is a declared output, not a dropped one (OMN-24932).

The runtime logged "no result applier" on every dispatch because the contract named the success event as a terminal
event while publish_topics stayed empty: the handler's output was dropped and the merged-change reading had no
result to see. These tests load the contract the way the runtime does (its own discovery and wiring, no raw YAML
read for the behaviour checks) and watch a result event reach the bus. They fail on the empty-publish shape and on a
runtime that ignores the two top-level keys.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
from omnibase_infra.runtime.contract_terminal_events import load_terminal_event_topics
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

from omnimarket.models.host_table_keys import ModelHostTableKeysIngestResult
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers import (
    handler_host_table_keys_ingest as ingest_module,
)
from omnimarket.nodes.node_host_table_keys_ingest_effect.handlers.store_host_keys import (
    StoreHostKeysInMemory,
)
from scripts.ci.check_contract_publish_parity import scan_publish_parity
from tests.nodes.node_host_table_compute.table_support import three_hosts
from tests.nodes.node_host_table_keys_ingest_effect.support import (
    PREFIX,
    overlay,
    record,
    registry_document,
)

pytestmark = pytest.mark.unit

NODE = "node_host_table_keys_ingest_effect"
NODES = Path(__file__).parents[3] / "src" / "omnimarket" / "nodes"
CONTRACT_PATH = NODES / NODE / "contract.yaml"
COMMAND_TOPIC = "onex.evt.omnimarket.overlay-store-loaded.v1"
RESULT_TOPIC = "onex.evt.omnimarket.host-table-keys-ingested.v1"
EFFECTS_PRINCIPAL = "${ONEX_ACL_DEV_RUNTIME_EFFECTS_PRINCIPAL}"


def raw_contract() -> dict[str, Any]:
    """The contract's own YAML, used only for the grant rows the runtime does not load."""
    loaded = yaml.safe_load(CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict)
    return loaded


def discovered() -> Any:
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert not manifest.errors, manifest.errors
    assert not manifest.skips, manifest.skips
    assert len(manifest.contracts) == 1
    return manifest.contracts[0]


def grants_of(principal: str, contract: dict[str, Any] | None = None) -> set[Any]:
    source = contract if contract is not None else raw_contract()
    return {
        (
            grant["resource_type"],
            grant["pattern"],
            grant["name"],
            grant["operation"],
        )
        for entry in source["broker_acl_grants"]
        if entry["principal"] == principal
        for grant in entry["grants"]
    }


# --- the runtime's own loader ------------------------------------------------------------------------------------


def test_loader_reads_the_result_topic_as_published_and_terminal() -> None:
    contract = discovered()
    assert contract.event_bus is not None
    assert RESULT_TOPIC in contract.event_bus.publish_topics
    assert contract.terminal_event == RESULT_TOPIC


def test_loader_result_route_selects_the_result_topic() -> None:
    """The fallback output topic the result applier is built on (None means no applier is built at all)."""
    contract = discovered()
    select = handler_wiring._select_dispatch_result_output_topic
    assert select(contract) == RESULT_TOPIC


def test_loader_terminal_topics_are_the_success_event_only() -> None:
    """runtime_dispatch.terminal_events declares success only, so no failure topic is left unpublished or ungranted."""
    declared = raw_contract()["runtime_dispatch"]["terminal_events"]
    assert declared == {"success": RESULT_TOPIC}
    assert load_terminal_event_topics(CONTRACT_PATH) == frozenset({RESULT_TOPIC})


def test_terminal_events_are_published_and_granted_write() -> None:
    contract = discovered()
    assert contract.event_bus is not None
    terminals = load_terminal_event_topics(CONTRACT_PATH)
    assert terminals
    assert terminals <= set(contract.event_bus.publish_topics)
    grants = grants_of(EFFECTS_PRINCIPAL)
    for topic in terminals:
        assert ("TOPIC", "LITERAL", topic, "WRITE") in grants
        assert ("TOPIC", "LITERAL", topic, "DESCRIBE") in grants


def parity_findings(copy_root: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """The publish parity gate's findings for a copy of this node alone, holding the given contract."""
    copy = copy_root / NODE
    shutil.copytree(NODES / NODE, copy, ignore=shutil.ignore_patterns("__pycache__"))
    (copy / "contract.yaml").write_text(yaml.safe_dump(contract))
    return {f.node: f.undeclared_topics for f in scan_publish_parity(copy_root)}


def test_terminal_event_key_is_what_the_publish_parity_gate_reads(
    tmp_path: Path,
) -> None:
    """The handler returns its result, so the parity gate sees no publish call; only the top-level key exempts the topic."""
    assert parity_findings(tmp_path, raw_contract()) == {}


def test_terminal_event_key_control_parity_gate_flags_the_node_without_it(
    tmp_path: Path,
) -> None:
    """Positive control for the empty finding list above: the same scan without the key finds the topic."""
    stripped = raw_contract()
    del stripped["terminal_event"]
    assert parity_findings(tmp_path, stripped) == {NODE: (RESULT_TOPIC,)}


# --- the applier on the bus -------------------------------------------------------------------------------------


async def test_applier_publishes_the_result_event_for_one_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One overlay-store record through the wired node ends as one ingest result on the declared result topic."""
    store = StoreHostKeysInMemory()
    monkeypatch.setattr(
        ingest_module, "load_structured_overlay", lambda *_a, **_k: overlay()
    )
    monkeypatch.setattr(
        ingest_module.StoreHostKeysValkey,
        "from_environment",
        classmethod(lambda _cls, *_a, **_k: store),
    )
    manifest = discover_contracts_from_paths([CONTRACT_PATH])
    assert not manifest.errors, manifest.errors
    bus = EventBusInmemory(environment="host-keys-ingest", group="host-keys-ingest")
    await bus.start()
    results: list[ModelHostTableKeysIngestResult] = []
    done = asyncio.Event()

    async def on_result(message: object) -> None:
        body = json.loads(bytes(getattr(message, "value", b"")))
        results.append(ModelHostTableKeysIngestResult.model_validate(body["payload"]))
        done.set()

    await bus.subscribe(RESULT_TOPIC, on_message=on_result, group_id="probe")
    engine = MessageDispatchEngine()
    report = await wire_from_manifest(
        ModelAutoWiringManifest(contracts=tuple(manifest.contracts)),
        engine,
        event_bus=bus,
        environment="local",
    )
    assert report.total_failed == 0
    engine.freeze()
    incoming = record(registry_document(*three_hosts()))
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=incoming.model_dump(mode="json"),
        correlation_id=uuid4(),
        envelope_timestamp=incoming.loaded_at,
    )
    await bus.publish(
        COMMAND_TOPIC, None, envelope.model_dump_json().encode("utf-8"), None
    )
    try:
        await asyncio.wait_for(done.wait(), timeout=30)
    finally:
        await bus.close()
    assert [(r.outcome, r.key) for r in results] == [("written", f"{PREFIX}:table")]
    assert store.get(f"{PREFIX}:table") is not None


# --- the contract's grants ------------------------------------------------------------------------------------


def test_contract_loader_accepts_the_two_top_level_keys() -> None:
    """The keys are present in the file and the runtime's loader carries terminal_event through (no parse error)."""
    declared = raw_contract()
    assert declared["externally_consumed_topics"] == [RESULT_TOPIC]
    assert declared["terminal_event"] == RESULT_TOPIC
    assert discovered().terminal_event == declared["terminal_event"]


def test_grant_effects_principal_writes_and_describes_the_result_topic() -> None:
    grants = grants_of(EFFECTS_PRINCIPAL)
    assert ("TOPIC", "LITERAL", RESULT_TOPIC, "WRITE") in grants
    assert ("TOPIC", "LITERAL", RESULT_TOPIC, "DESCRIBE") in grants


def test_grant_result_topic_has_no_bus_reader_so_no_reader_grant_is_owed() -> None:
    """The merged-change reading reads the runtime container's log, not the topic, so no principal needs READ.

    If a contract ever subscribes to the result topic, its principal must hold READ and DESCRIBE on it in a
    contract's grants; this test then names the missing row instead of passing on an empty set.
    """
    subscribers: list[str] = []
    for path in sorted(NODES.glob("*/contract.yaml")):
        text = path.read_text()
        if RESULT_TOPIC not in text:
            continue
        loaded = yaml.safe_load(text)
        assert isinstance(loaded, dict)
        bus = loaded.get("event_bus") or {}
        if RESULT_TOPIC in (bus.get("subscribe_topics") or []):
            subscribers.append(path.parent.name)
    assert subscribers == []
    reader = yaml.safe_load(
        (NODES / "node_merged_change_reading_effect" / "contract.yaml").read_text()
    )
    assert reader["event_bus"]["subscribe_topics"] == []
    assert "Published output event to " in reader["runtime"]["log"]["published_pattern"]


# --- the evidence contract ---------------------------------------------------------------------------------------

EVIDENCE_PATH = Path(__file__).parents[3] / "contracts" / "OMN-24932.yaml"
OWN_FALSIFIERS = {
    "AC1": "Published output event to " + RESULT_TOPIC,
    "AC2": "omnimarket#5924",
}


def evidence_items() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(EVIDENCE_PATH.read_text())
    return cast("list[dict[str, Any]]", loaded["dod_evidence"])


def test_contract_ac1_and_ac2_are_not_bound_to_the_yaml_shape_test() -> None:
    """A YAML-shape pytest cannot fail for a missing h201 dispatch or a missing STATUS row."""
    for item in evidence_items():
        bound = set(item.get("binds_ac") or [])
        if bound & {"AC1", "AC2"}:
            commands = " ".join(c["check_value"] for c in item["checks"])
            assert "pytest" not in commands, item["id"]


def test_contract_ac1_and_ac2_carry_their_own_falsifier() -> None:
    items = evidence_items()
    for label, needle in OWN_FALSIFIERS.items():
        owners = [
            item
            for item in items
            if item["id"].startswith(f"dod-omn24932-{label.lower()}-")
        ]
        assert len(owners) == 1, label
        assert [c["check_type"] for c in owners[0]["checks"]] == ["command"]
        assert needle in owners[0]["checks"][0]["check_value"], label
        assert owners[0]["execution_scope"] == "local_done_gate", label
        # The h201 read waits on the merges and the rebuild, and the description says so.
        assert "overlays#590" in owners[0]["description"] or label == "AC2"
        assert "local Done gate only" in owners[0]["description"], label


def test_contract_ac3_is_bound_to_the_runtime_loading_tests() -> None:
    (ac3,) = [i for i in evidence_items() if i.get("binds_ac") == ["AC3"]]
    assert "test_result_topic_declared.py" in ac3["checks"][0]["check_value"]
