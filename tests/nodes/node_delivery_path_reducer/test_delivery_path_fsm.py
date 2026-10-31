# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delivery path contract, persisted deadlines, receipt identity and bindings."""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import permutations
from pathlib import Path

import pytest
import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.validation.validator_fsm_analysis import analyze_fsm
from pydantic import ValidationError

from omnimarket.models.binding_conformance.enum_binding_rule import EnumBindingRule
from omnimarket.models.binding_conformance.enum_binding_source_kind import (
    EnumBindingSourceKind,
)
from omnimarket.models.binding_conformance.model_binding_conformance_request import (
    ModelBindingConformanceRequest,
)
from omnimarket.models.binding_conformance.model_binding_source import (
    ModelBindingSource,
)
from omnimarket.nodes.node_binding_conformance_compute.handlers.handler_binding_conformance import (
    HandlerBindingConformance,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_delivery_path_reducer import NodeDeliveryPathReducer
from omnimarket.nodes.node_delivery_path_reducer.handlers.handler_delivery_path import (
    CONTRACT_PATH,
    HandlerDeliveryPath,
    build_context,
)
from omnimarket.nodes.node_delivery_path_reducer.models.model_delivery_path import (
    ModelDeliveryConfig,
    ModelDeliveryConsumer,
    ModelDeliveryMerge,
    ModelDeliveryPathEvent,
    ModelDeliveryPathRecord,
    ModelDeliveryPathRequest,
    ModelDeliveryPathResult,
    ModelDeliveryReceipt,
    ModelDeliveryReference,
    ModelDeliveryTag,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.handlers.handler_fsm_single_source_check import (
    HandlerFsmSingleSourceCheck,
)
from omnimarket.nodes.node_fsm_single_source_check_compute.models.model_fsm_single_source_check import (
    ModelFsmSingleSourceCheckRequest,
    ModelSourceText,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[3]
NODE = "node_delivery_path_reducer"
NODE_DIR = REPO / "src/omnimarket/nodes" / NODE
FIXTURE = Path(__file__).parent / "fixtures/section_7_3_state_machine.yaml"
ACTIVE_STATES = (
    "declared",
    "tag_pending",
    "tag_resolved",
    "installing",
    "verified",
    "stale",
)


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _fold(
    handler: HandlerDeliveryPath,
    record: ModelDeliveryPathRecord,
    trigger: str | None,
    now: datetime = NOW,
    **event_fields: object,
) -> ModelDeliveryPathResult:
    event = (
        None
        if trigger is None
        else ModelDeliveryPathEvent.model_validate({"trigger": trigger, **event_fields})
    )
    return handler.handle(
        ModelDeliveryPathRequest(path_id="path-1", record=record, event=event, now=now)
    )


def _record(state: str = "tag_resolved", **updates: object) -> ModelDeliveryPathRecord:
    return ModelDeliveryPathRecord.model_validate(
        {
            "state": state,
            "expected_since": NOW,
            "merge_commit": "M1",
            "target_tag": "T1",
            "artifact_identity": "sha-1",
            "target_generation": 1,
            **updates,
        }
    )


@pytest.fixture
def contract_with_limit(tmp_path: Path) -> Callable[[int], Path]:
    def write(minutes: int) -> Path:
        contract = yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))
        contract["config"]["delivery"]["stale_after_minutes"] = minutes
        path = tmp_path / f"contract-{minutes}.yaml"
        path.write_text(yaml.safe_dump(contract), encoding="utf-8")
        return path

    return write


def _contract() -> dict:
    return yaml.safe_load(CONTRACT_PATH.read_text("utf-8"))


def _config() -> ModelDeliveryConfig:
    return ModelDeliveryConfig.model_validate(_contract()["config"]["delivery"])


def _receipt(record: ModelDeliveryPathRecord) -> ModelDeliveryReceipt:
    return ModelDeliveryReceipt(
        tag=record.target_tag,
        target_generation=record.target_generation,
        artifact_identity=record.artifact_identity,
    )


def _run_local_config() -> ModelRunLocalConfig:
    contract = yaml.safe_load(
        (
            REPO / "src/omnimarket/nodes/node_binding_parity_orchestrator/contract.yaml"
        ).read_text("utf-8")
    )
    return ModelRunLocalConfig.model_validate(contract["config"]["run_local"])


def _binding_data() -> dict:
    return yaml.safe_load(
        (
            REPO
            / "tests/nodes/node_binding_conformance_compute/data/binding_table.yaml"
        ).read_text("utf-8")
    )


def _real_sources(node: str) -> tuple[ModelBindingSource, ...]:
    root = REPO / "src/omnimarket/nodes" / node
    sources = [
        ModelBindingSource(
            node=node,
            path=str((root / "contract.yaml").relative_to(REPO)),
            kind=EnumBindingSourceKind.CONTRACT,
            text=(root / "contract.yaml").read_text("utf-8"),
        )
    ]
    files = [(EnumBindingSourceKind.HANDLER, f) for f in root.rglob("handler*.py")]
    files += [
        (EnumBindingSourceKind.HANDLER, f) for f in (root / "handlers").rglob("*.py")
    ]
    model_roots = [
        root / "models",
        *(REPO / d for d in _binding_data()["model_dirs"].get(node, [])),
    ]
    files += [(EnumBindingSourceKind.MODEL, f) for f in root.glob("models.py")]
    files += [
        (EnumBindingSourceKind.MODEL, f)
        for directory in model_roots
        for f in directory.rglob("*.py")
    ]
    seen: set[Path] = set()
    for kind, file in sorted(files, key=lambda pair: pair[1]):
        if file in seen or file.name == "__init__.py":
            continue
        seen.add(file)
        sources.append(
            ModelBindingSource(
                node=node,
                path=str(file.relative_to(REPO)),
                kind=kind,
                text=file.read_text("utf-8"),
            )
        )
    return tuple(sources)


def _real_request() -> ModelBindingConformanceRequest:
    data = _binding_data()
    return ModelBindingConformanceRequest.model_validate(
        {
            "checked_nodes": [NODE],
            "protocol_table": {"protocols": data["protocols"], "rows": data["rows"]},
            "concrete_bindings": data["concrete_bindings"],
            "pure_forbidden_modules": data["pure_forbidden_modules"],
            "private_names": [
                {"kind": "repository", "value": "acme-widgets"},
                {"kind": "host", "value": "box-17.fixture.invalid"},
                {"kind": "organization", "value": "acme-fixture-org"},
            ],
            "sources": _real_sources(NODE),
        }
    )


def test_state_machine_equals_section_7_3_fragment() -> None:
    assert (
        _contract()["state_machine"]
        == yaml.safe_load(FIXTURE.read_text("utf-8"))["state_machine"]
    )


def test_state_machine_loads_and_passes_analyze_fsm() -> None:
    result = analyze_fsm(
        ModelFSMSubcontract.model_validate(_contract()["state_machine"])
    )
    assert not result.unreachable_states
    assert not result.dead_transitions
    assert not result.missing_transitions
    assert not result.ambiguous_transitions
    assert not result.cycles_without_exit
    assert not result.errors
    assert result.is_valid


def test_reference_branch_is_refused() -> None:
    assert (
        _fold(
            HandlerDeliveryPath(),
            ModelDeliveryPathRecord(),
            "path_checked",
            reference=ModelDeliveryReference(ref_kind="branch", reference_allowed=True),
        ).state
        == "refused"
    )


def test_reference_sha_is_refused() -> None:
    assert (
        _fold(
            HandlerDeliveryPath(),
            ModelDeliveryPathRecord(),
            "path_checked",
            reference=ModelDeliveryReference(ref_kind="sha", reference_allowed=True),
        ).state
        == "refused"
    )


def test_reference_hand_kept_version_is_refused() -> None:
    assert (
        _fold(
            HandlerDeliveryPath(),
            ModelDeliveryPathRecord(),
            "path_checked",
            reference=ModelDeliveryReference(
                ref_kind="hand_kept_version", reference_allowed=True
            ),
        ).state
        == "refused"
    )


def test_nothing_leaves_refused() -> None:
    handler = HandlerDeliveryPath()
    record = _record("refused")
    triggers = {t["trigger"] for t in _contract()["state_machine"]["transitions"]} | {
        "stale_limit_reached",
        None,
    }
    contents = (
        {},
        {
            "tag": ModelDeliveryTag(
                tag="T2",
                artifact_identity="sha-2",
                merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
            )
        },
        {"merge": ModelDeliveryMerge(merge_commit="M2", merged_at=_at(1))},
        {"receipt": _receipt(record)},
    )
    for trigger in triggers:
        for fields in contents:
            result = _fold(handler, record, trigger, _at(100), **fields)
            assert result.state == "refused"
            assert not result.moved
            assert not result.accepted
            assert result.record == record
            assert not result.triggers
            assert not result.decision_rows


def test_waiting_path_missing_tag_reaches_stale_and_writes_one_row() -> None:
    handler = HandlerDeliveryPath()
    pending = _fold(
        handler,
        ModelDeliveryPathRecord(),
        "path_checked",
        reference=ModelDeliveryReference(
            ref_kind="dev_merge_tag", reference_allowed=True
        ),
    )
    assert pending.state == "tag_pending"
    assert pending.record.expected_since == NOW
    result = _fold(handler, pending.record, None, _at(_config().stale_after_minutes))
    assert result.state == "stale"
    assert result.moved
    assert not result.accepted
    assert result.triggers == (_config().limit_trigger,)
    assert len(result.decision_rows) == 1
    row = result.decision_rows[0]
    assert row.path_id == "path-1"
    assert row.from_state == "tag_pending"
    assert row.decision == "stale"
    assert row.expected_since == NOW
    assert row.stale_after_minutes == _config().stale_after_minutes
    assert row.target_tag is None
    assert row.target_generation == 0
    assert row.decided_at == result.decided_at
    again = _fold(handler, result.record, None, _at(200))
    assert not again.decision_rows
    assert not again.triggers


def test_tag_appears_after_stale_brings_it_back_and_install_verifies() -> None:
    handler = HandlerDeliveryPath()
    stale = _fold(
        handler,
        _record("tag_pending", target_tag=None, artifact_identity=None),
        None,
        _at(60),
    )
    resolved = _fold(
        handler,
        stale.record,
        "tag_seen",
        _at(61),
        tag=ModelDeliveryTag(
            tag="T2",
            artifact_identity="sha-2",
            merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
        ),
    )
    assert resolved.state == "tag_resolved"
    installing = _fold(handler, resolved.record, "install_started", _at(62))
    assert installing.state == "installing"
    result = _fold(
        handler,
        installing.record,
        "install_receipt_read",
        _at(63),
        receipt=_receipt(installing.record),
    )
    assert result.state == "verified"
    assert result.record.expected_since == NOW
    assert result.record.stale_decided_for == NOW
    assert not result.decision_rows


def test_install_started_from_stale_needs_target_resolved() -> None:
    handler = HandlerDeliveryPath()
    missing = _fold(
        handler,
        _record("stale", target_tag=None, artifact_identity=None),
        "install_started",
    )
    assert missing.state == "stale"
    assert not missing.accepted
    assert missing.failed_conditions
    assert missing.error
    assert _fold(handler, _record("stale"), "install_started").state == "installing"


def test_path_whose_tag_never_appears_does_not_stay_pending_forever() -> None:
    result = _fold(
        HandlerDeliveryPath(),
        _record("tag_pending", target_tag=None, artifact_identity=None),
        None,
        _at(10000),
    )
    assert result.state == "stale"


def test_older_tag_receipts_across_five_retries_reach_stale_at_the_limit(
    contract_with_limit: Callable[[int], Path],
) -> None:
    handler = HandlerDeliveryPath(contract_with_limit(30))
    record = _record(target_tag="T2", artifact_identity="sha-2", target_generation=2)
    stale_rounds = []
    for round_number in range(1, 6):
        minute = round_number * 7
        started = _fold(handler, record, "install_started", _at(minute - 7))
        result = _fold(
            handler,
            started.record,
            "install_receipt_read",
            _at(minute),
            receipt=ModelDeliveryReceipt(
                tag="T1", target_generation=1, artifact_identity="sha-1"
            ),
        )
        record = result.record
        assert record.expected_since == NOW
        assert record.target_tag == "T2"
        if minute < 30:
            assert result.state == "tag_resolved"
            assert not result.decision_rows
        else:
            assert result.state == "stale"
            assert len(result.decision_rows) == 1
            stale_rounds.append(round_number)
    assert stale_rounds == [5]


def test_missing_receipt_reaches_stale_at_the_limit() -> None:
    result = _fold(HandlerDeliveryPath(), _record("installing"), None, _at(61))
    assert result.state == "stale"
    assert len(result.decision_rows) == 1


def test_installing_has_its_own_stale_limit_transition() -> None:
    assert any(
        t["from_state"] == "installing"
        and t["trigger"] == _config().limit_trigger
        and t["to_state"] == _config().decision_state
        for t in _contract()["state_machine"]["transitions"]
    )


def test_mismatching_receipt_returns_to_tag_resolved() -> None:
    record = _record("installing")
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "install_receipt_read",
        _at(5),
        receipt=ModelDeliveryReceipt(tag="older"),
    )
    assert result.state == "tag_resolved"
    assert result.accepted
    assert result.record == record.model_copy(update={"state": "tag_resolved"})
    assert not result.decision_rows


def test_verified_path_with_untagged_newer_merge_goes_pending_then_stale_from_that_merge() -> (
    None
):
    handler = HandlerDeliveryPath()
    m2 = _at(120)
    pending = _fold(
        handler,
        _record("verified"),
        "newer_merge_seen",
        _at(125),
        merge=ModelDeliveryMerge(merge_commit="M2", merged_at=m2),
    )
    assert pending.state == "tag_pending"
    assert pending.record.expected_since == m2
    assert pending.record.merge_commit == "M2"
    assert pending.record.target_tag is None
    assert pending.record.artifact_identity is None
    assert pending.record.target_generation == 2
    assert _fold(handler, pending.record, None, _at(179)).state == "tag_pending"
    assert _fold(handler, pending.record, None, _at(180)).state == "stale"


@pytest.mark.parametrize("state", ACTIVE_STATES)
def test_path_checked_refuses_from_every_active_state(state: str) -> None:
    result = _fold(
        HandlerDeliveryPath(),
        _record(state),
        "path_checked",
        reference=ModelDeliveryReference(
            ref_kind="candidate_tag", reference_allowed=False
        ),
    )
    assert result.state == "refused"
    assert result.accepted
    assert result.moved


@pytest.mark.parametrize("state", ACTIVE_STATES)
def test_path_checked_allowed_moves_declared_to_pending_and_leaves_others_unchanged(
    state: str,
) -> None:
    record = _record(state)
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "path_checked",
        reference=ModelDeliveryReference(
            ref_kind="dev_merge_tag", reference_allowed=True
        ),
    )
    assert result.state == ("tag_pending" if state == "declared" else state)
    assert result.moved == (state == "declared")
    assert result.accepted
    assert result.record.target_tag == record.target_tag
    assert result.record.target_generation == record.target_generation
    assert result.record.expected_since == record.expected_since


@pytest.mark.parametrize("kind", ["dev_merge_tag", "candidate_tag"])
def test_allowed_kinds_dev_merge_tag_and_candidate_tag(kind: str) -> None:
    assert (
        _fold(
            HandlerDeliveryPath(),
            ModelDeliveryPathRecord(),
            "path_checked",
            reference=ModelDeliveryReference(ref_kind=kind, reference_allowed=True),
        ).state
        == "tag_pending"
    )


@pytest.mark.parametrize("state", ["tag_pending", "installing"])
@pytest.mark.parametrize(
    ("kind", "allowed"), [("branch", True), ("candidate_tag", False)]
)
def test_reference_changed_during_pending_and_during_installation(
    state: str, kind: str, allowed: bool
) -> None:
    assert (
        _fold(
            HandlerDeliveryPath(),
            _record(state),
            "path_checked",
            reference=ModelDeliveryReference(ref_kind=kind, reference_allowed=allowed),
        ).state
        == "refused"
    )


@pytest.mark.parametrize("tag", ["another", None])
def test_consumer_off_tag_leaves_verified_for_stale(tag: str | None) -> None:
    result = _fold(
        HandlerDeliveryPath(),
        _record("verified"),
        "consumer_read",
        consumer=ModelDeliveryConsumer(tag=tag),
    )
    assert result.state == "stale"
    assert len(result.decision_rows) == 1
    assert result.decision_rows[0].from_state == "verified"
    assert result.triggers == ("consumer_read",)


def test_consumer_on_tag_stays_verified() -> None:
    result = _fold(
        HandlerDeliveryPath(),
        _record("verified"),
        "consumer_read",
        consumer=ModelDeliveryConsumer(tag="T1"),
    )
    assert result.state == "verified"
    assert not result.moved
    assert result.accepted
    assert not result.decision_rows


def test_receipt_for_superseded_target_never_reaches_verified() -> None:
    handler = HandlerDeliveryPath()
    a = _record("installing")
    b = _fold(
        handler,
        a,
        "newer_merge_seen",
        _at(1),
        merge=ModelDeliveryMerge(merge_commit="M2", merged_at=_at(1)),
    )
    result = _fold(
        handler, b.record, "install_receipt_read", _at(2), receipt=_receipt(a)
    )
    assert result.state == "tag_pending"
    assert not result.accepted
    assert result.record.merge_commit == "M2"
    assert result.record.target_tag is None


def test_superseded_receipt_after_newer_tag_seen() -> None:
    handler = HandlerDeliveryPath()
    a = _record("installing")
    b = _fold(
        handler,
        a,
        "newer_tag_seen",
        tag=ModelDeliveryTag(
            tag="T2",
            artifact_identity="sha-2",
            merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
        ),
    )
    assert b.state == "tag_resolved"
    assert b.record.target_generation > a.target_generation
    early = _fold(handler, b.record, "install_receipt_read", receipt=_receipt(a))
    assert early.state == "tag_resolved"
    assert not early.accepted
    started = _fold(handler, early.record, "install_started")
    result = _fold(handler, started.record, "install_receipt_read", receipt=_receipt(a))
    assert result.state == "tag_resolved"
    assert result.accepted
    assert result.record.target_tag == "T2"
    assert result.record.artifact_identity == "sha-2"


def test_superseded_receipt_survives_restart() -> None:
    events = (
        (
            "newer_tag_seen",
            {
                "tag": ModelDeliveryTag(
                    tag="T2",
                    artifact_identity="sha-2",
                    merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
                )
            },
        ),
        ("install_started", {}),
        ("install_receipt_read", {"receipt": _receipt(_record("installing"))}),
    )
    memory = restarted = _record("installing")
    handler = HandlerDeliveryPath()
    for trigger, fields in events:
        expected = _fold(handler, memory, trigger, **fields)
        reloaded = ModelDeliveryPathRecord.model_validate_json(
            restarted.model_dump_json()
        )
        assert reloaded == restarted
        assert reloaded.target_generation == restarted.target_generation
        assert reloaded.artifact_identity == restarted.artifact_identity
        actual = _fold(HandlerDeliveryPath(), reloaded, trigger, **fields)
        assert actual.model_dump_json() == expected.model_dump_json()
        memory, restarted = expected.record, actual.record
    assert restarted.state == "tag_resolved"
    assert restarted.target_tag == "T2"


def test_concurrent_sync_duplicates_and_orderings() -> None:
    a = _record("installing")
    for order in set(permutations(("merge", "merge", "receipt"))):
        handler = HandlerDeliveryPath()
        record = a
        for kind in order:
            if kind == "merge":
                before = record
                result = _fold(
                    handler,
                    record,
                    "newer_merge_seen",
                    _at(2),
                    merge=ModelDeliveryMerge(merge_commit="M2", merged_at=_at(1)),
                )
                if before.merge_commit == "M2":
                    assert not result.accepted
                    assert not result.moved
                    assert result.error is None
                    assert result.record == before
                    assert not result.triggers
            else:
                result = _fold(
                    handler, record, "install_receipt_read", _at(2), receipt=_receipt(a)
                )
                if record.merge_commit == "M2":
                    assert result.state != "verified"
            record = result.record
        assert record.state == "tag_pending"
        assert record.merge_commit == "M2"
        assert record.target_generation == a.target_generation + 1


def test_tag_with_changed_artifact_identity_is_rejected() -> None:
    record = _record()
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "newer_tag_seen",
        _at(100),
        tag=ModelDeliveryTag(
            tag="T1",
            artifact_identity="moved-sha",
            merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
        ),
    )
    assert not result.accepted
    assert not result.moved
    assert result.error == "artifact identity of tag T1 changed"
    assert result.record == record
    assert not result.triggers
    assert not result.decision_rows


@pytest.mark.parametrize(
    ("generation", "identity"),
    [(0, "sha-1"), (1, "other-sha"), (None, "sha-1"), (1, None)],
)
def test_receipt_target_current_needs_generation_and_identity(
    generation: int | None, identity: str | None
) -> None:
    result = _fold(
        HandlerDeliveryPath(),
        _record("installing"),
        "install_receipt_read",
        receipt=ModelDeliveryReceipt(
            tag="T1", target_generation=generation, artifact_identity=identity
        ),
    )
    assert result.state == "tag_resolved"


def test_stale_after_minutes_is_read_from_the_contract(
    contract_with_limit: Callable[[int], Path],
) -> None:
    record = _record("tag_pending", target_tag=None, artifact_identity=None)
    assert _config().stale_after_minutes == 60
    assert (
        _fold(HandlerDeliveryPath(contract_with_limit(30)), record, None, _at(45)).state
        == "stale"
    )
    assert (
        _fold(
            HandlerDeliveryPath(contract_with_limit(600)), record, None, _at(45)
        ).state
        == "tag_pending"
    )


def test_handler_source_holds_no_limit_number() -> None:
    source = (NODE_DIR / "handlers/handler_delivery_path.py").read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, int):
            assert node.value in (0, 1)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "stale_after_minutes" not in node.value
        if isinstance(node, ast.Name):
            assert node.id != "stale_after_minutes"
    accesses = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and n.attr == "stale_after_minutes"
    ]
    assert accesses
    assert source.count("stale_after_minutes") == len(accesses)


def test_state_machine_single_source_check_passes() -> None:
    result = HandlerFsmSingleSourceCheck().handle(
        ModelFsmSingleSourceCheckRequest(
            contract_text=CONTRACT_PATH.read_text("utf-8"),
            sources=tuple(
                ModelSourceText(
                    name=str(p.relative_to(REPO)), text=p.read_text("utf-8")
                )
                for p in sorted(NODE_DIR.rglob("*.py"))
            ),
        )
    )
    assert not result.issues
    assert result.exit_code == 0


def test_contract_has_no_protocol_dependencies() -> None:
    for path in (CONTRACT_PATH, NODE_DIR / "metadata.yaml"):
        assert "protocol_dependencies" not in yaml.safe_load(path.read_text("utf-8"))


def test_in_process_and_entry_wrapper_are_byte_identical() -> None:
    request = ModelDeliveryPathRequest(
        path_id="path-1",
        record=_record("installing"),
        event=ModelDeliveryPathEvent(
            trigger="install_receipt_read", receipt=_receipt(_record())
        ),
        now=NOW,
    )
    assert (
        HandlerDeliveryPath().handle(request).model_dump_json()
        == NodeDeliveryPathReducer().handle(request).model_dump_json()
    )


def test_byte_identical_through_the_local_in_process_entry_of_the_parity_harness() -> (
    None
):
    config = _run_local_config()
    direct_record = bus_record = ModelDeliveryPathRecord()
    events = (
        ModelDeliveryPathEvent(
            trigger="path_checked",
            reference=ModelDeliveryReference(
                ref_kind="dev_merge_tag", reference_allowed=True
            ),
        ),
        ModelDeliveryPathEvent(
            trigger="tag_seen",
            tag=ModelDeliveryTag(
                tag="T1",
                artifact_identity="sha-1",
                merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
            ),
        ),
        ModelDeliveryPathEvent(trigger="install_started"),
        ModelDeliveryPathEvent(
            trigger="install_receipt_read",
            receipt=ModelDeliveryReceipt(
                tag="T1", target_generation=1, artifact_identity="sha-1"
            ),
        ),
    )
    for event in events:
        request = ModelDeliveryPathRequest(
            path_id="path-1", record=direct_record, event=event, now=NOW
        )
        direct = HandlerDeliveryPath().handle(request)
        bus_request = request.model_copy(update={"record": bus_record})
        actual = asyncio.run(
            run_local(
                NODE,
                "delivery_path_fold",
                json.loads(bus_request.model_dump_json()),
                config,
            )
        )
        assert actual == json.loads(direct.model_dump_json())
        direct_record = direct.record
        bus_record = ModelDeliveryPathResult.model_validate(actual).record
    assert bus_record.state == "verified"


def test_conformance_check_passes_for_this_node() -> None:
    assert NODE in _binding_data()["checked_nodes"]
    result = HandlerBindingConformance().handle(_real_request())
    assert result.conforming is True, result.issues


def test_conformance_check_fails_when_a_protocol_dependency_is_added() -> None:
    request = _real_request()
    bus = next(
        p["protocol"] for p in _binding_data()["protocols"] if p["code"] == "BUS"
    )
    sources = tuple(
        s.model_copy(
            update={
                "text": s.text
                + yaml.safe_dump(
                    {
                        "protocol_dependencies": [
                            {
                                "name": "ProtocolEventBus",
                                "protocol": bus,
                                "required": True,
                            }
                        ]
                    }
                )
            }
        )
        if s.kind is EnumBindingSourceKind.CONTRACT
        else s
        for s in request.sources
    )
    result = HandlerBindingConformance().handle(
        request.model_copy(update={"sources": sources})
    )
    assert result.conforming is False
    assert {i.rule for i in result.issues} == {EnumBindingRule.CONTRACT_EXTRA_PROTOCOL}


@pytest.mark.parametrize(
    "event",
    [
        None,
        ModelDeliveryPathEvent(trigger="path_checked"),
        ModelDeliveryPathEvent(
            trigger="path_checked",
            reference=ModelDeliveryReference(ref_kind="branch", reference_allowed=True),
        ),
        ModelDeliveryPathEvent(
            trigger="path_checked",
            reference=ModelDeliveryReference(
                ref_kind="dev_merge_tag", reference_allowed=True
            ),
        ),
        ModelDeliveryPathEvent(
            trigger="install_receipt_read",
            receipt=ModelDeliveryReceipt(
                tag="T1", target_generation=1, artifact_identity="sha-1"
            ),
        ),
        ModelDeliveryPathEvent(
            trigger="consumer_read", consumer=ModelDeliveryConsumer(tag="T1")
        ),
    ],
)
@pytest.mark.parametrize("record", [_record(), ModelDeliveryPathRecord()])
def test_context_builder_writes_real_booleans(
    event: ModelDeliveryPathEvent | None, record: ModelDeliveryPathRecord
) -> None:
    context = build_context(record, event, _config())
    assert set(context) == {
        "ref_kind",
        "reference_allowed",
        "receipt_matches_tag",
        "receipt_target_current",
        "consumer_on_tag",
        "target_resolved",
    }
    assert isinstance(context["ref_kind"], str)
    assert all(
        type(value) is bool for key, value in context.items() if key != "ref_kind"
    )
    assert context["target_resolved"] == (record.target_tag is not None)
    if event is None or event.reference is None:
        assert context["reference_allowed"] is False
    if event is None or event.consumer is None:
        assert context["consumer_on_tag"] is False


def test_terminal_event_is_declared_in_the_contract() -> None:
    contract = _contract()
    terminal = contract["runtime_dispatch"]["terminal_events"]["success"]
    assert terminal == "onex.evt.omnimarket.delivery-path-folded.v1"
    assert terminal == contract["terminal_event"]
    assert terminal in contract["event_bus"]["publish_topics"]


def test_handler_runs_inside_a_running_loop() -> None:
    async def run() -> None:
        assert (
            _fold(HandlerDeliveryPath(), _record(), "install_started").state
            == "installing"
        )

    asyncio.run(run())


def test_identical_tag_replay_still_checks_clock() -> None:
    record = _record()
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "newer_tag_seen",
        _at(60),
        tag=ModelDeliveryTag(
            tag="T1",
            artifact_identity="sha-1",
            merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
        ),
    )
    assert not result.accepted
    assert result.error is None
    assert result.state == "stale"
    assert result.record.target_generation == record.target_generation
    assert result.triggers == (_config().limit_trigger,)


def test_undeclared_event_still_checks_clock() -> None:
    result = _fold(HandlerDeliveryPath(), _record("tag_pending"), "unknown", _at(60))
    assert not result.accepted
    assert result.error
    assert result.state == "stale"
    assert len(result.decision_rows) == 1


def test_new_merge_resets_stale_marker_and_starts_a_new_deadline() -> None:
    record = _record("verified", stale_decided_for=NOW)
    handler = HandlerDeliveryPath()
    pending = _fold(
        handler,
        record,
        "newer_merge_seen",
        _at(70),
        merge=ModelDeliveryMerge(merge_commit="M2", merged_at=_at(70)),
    )
    assert pending.record.stale_decided_for is None
    result = _fold(handler, pending.record, None, _at(130))
    assert result.state == "stale"
    assert len(result.decision_rows) == 1
    assert result.record.stale_decided_for == _at(70)


def test_older_merge_does_not_replace_the_current_anchor_or_target() -> None:
    record = _record("verified")
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "newer_merge_seen",
        merge=ModelDeliveryMerge(merge_commit="older", merged_at=_at(-1)),
    )
    assert result.state == "verified"
    assert result.record == record
    assert not result.accepted
    assert not result.moved
    assert result.error is None
    assert not result.triggers
    assert not result.decision_rows


@pytest.mark.parametrize("already_decided", [False, True])
def test_newer_tag_from_verified_reanchors_to_the_tags_merge(
    already_decided: bool,
) -> None:
    handler = HandlerDeliveryPath()
    old_anchor = NOW - timedelta(hours=5)
    record = _record(
        "verified",
        expected_since=old_anchor,
        stale_decided_for=old_anchor if already_decided else None,
    )
    merge = ModelDeliveryMerge(merge_commit="M2", merged_at=_at(-1))
    resolved = _fold(
        handler,
        record,
        "newer_tag_seen",
        tag=ModelDeliveryTag(tag="T2", artifact_identity="sha-2", merge=merge),
    )
    assert resolved.accepted
    assert resolved.error is None
    assert resolved.state == "tag_resolved"
    assert resolved.triggers == ("newer_tag_seen",)
    assert not resolved.decision_rows
    assert resolved.record.expected_since == merge.merged_at
    assert resolved.record.merge_commit == merge.merge_commit
    assert resolved.record.target_tag == "T2"
    assert resolved.record.artifact_identity == "sha-2"
    assert resolved.record.target_generation == record.target_generation + 1
    assert resolved.record.stale_decided_for is None

    deadline = merge.merged_at + timedelta(minutes=_config().stale_after_minutes)
    before = _fold(handler, resolved.record, None, deadline - timedelta(minutes=1))
    assert before.state == "tag_resolved"
    assert before.record == resolved.record
    assert not before.decision_rows
    stale = _fold(handler, before.record, None, deadline)
    assert stale.state == "stale"
    assert stale.triggers == (_config().limit_trigger,)
    assert len(stale.decision_rows) == 1
    assert stale.decision_rows[0].expected_since == merge.merged_at
    assert stale.decision_rows[0].target_tag == "T2"
    assert stale.decision_rows[0].target_generation == record.target_generation + 1
    assert stale.decision_rows[0].from_state == "tag_resolved"
    assert stale.record.stale_decided_for == merge.merged_at
    again = _fold(handler, stale.record, None, deadline + timedelta(minutes=1))
    assert again.record == stale.record
    assert not again.decision_rows


@pytest.mark.parametrize("merged_minutes", [-1, 0])
def test_merge_not_newer_than_the_anchor_is_a_no_op(merged_minutes: int) -> None:
    record = _record()
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "newer_merge_seen",
        merge=ModelDeliveryMerge(merge_commit="M2", merged_at=_at(merged_minutes)),
    )
    assert result.state == "tag_resolved"
    assert result.record == record
    assert not result.accepted
    assert not result.moved
    assert result.error is None
    assert not result.failed_conditions
    assert not result.triggers
    assert not result.decision_rows


@pytest.mark.parametrize("merged_minutes", [-1, 0])
def test_merge_not_newer_than_the_anchor_is_a_no_op_still_checks_clock(
    merged_minutes: int,
) -> None:
    record = _record()
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "newer_merge_seen",
        _at(_config().stale_after_minutes),
        merge=ModelDeliveryMerge(merge_commit="M2", merged_at=_at(merged_minutes)),
    )
    assert not result.accepted
    assert result.error is None
    assert result.state == "stale"
    assert result.record == record.model_copy(
        update={"state": "stale", "stale_decided_for": NOW}
    )
    assert result.triggers == (_config().limit_trigger,)
    assert len(result.decision_rows) == 1
    assert result.decision_rows[0].from_state == "tag_resolved"


@pytest.mark.parametrize("merged_minutes", [-1, 0])
def test_tag_seen_with_older_merge_keeps_the_anchor(merged_minutes: int) -> None:
    record = _record(
        "tag_pending", target_tag=None, artifact_identity=None, stale_decided_for=NOW
    )
    result = _fold(
        HandlerDeliveryPath(),
        record,
        "tag_seen",
        tag=ModelDeliveryTag(
            tag="T2",
            artifact_identity="sha-2",
            merge=ModelDeliveryMerge(
                merge_commit="older", merged_at=_at(merged_minutes)
            ),
        ),
    )
    assert result.accepted
    assert result.error is None
    assert result.state == "tag_resolved"
    assert result.record == record.model_copy(
        update={
            "state": "tag_resolved",
            "target_tag": "T2",
            "artifact_identity": "sha-2",
            "target_generation": record.target_generation + 1,
        }
    )
    assert result.triggers == ("tag_seen",)
    assert not result.decision_rows


def test_tag_seen_without_an_anchor_uses_the_tags_merge_time() -> None:
    merge = ModelDeliveryMerge(merge_commit="M2", merged_at=_at(-1))
    result = _fold(
        HandlerDeliveryPath(),
        ModelDeliveryPathRecord(state="tag_pending"),
        "tag_seen",
        tag=ModelDeliveryTag(tag="T2", artifact_identity="sha-2", merge=merge),
    )
    assert result.accepted
    assert result.state == "tag_resolved"
    assert result.record.expected_since == merge.merged_at
    assert result.record.merge_commit == merge.merge_commit
    assert result.record.target_tag == "T2"
    assert result.record.target_generation == 1
    assert not result.decision_rows


def test_event_cannot_carry_both_tag_and_merge() -> None:
    with pytest.raises(ValidationError):
        ModelDeliveryPathEvent(
            trigger="newer_tag_seen",
            tag=ModelDeliveryTag(
                tag="T1",
                artifact_identity="sha-1",
                merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
            ),
            merge=ModelDeliveryMerge(merge_commit="M1", merged_at=NOW),
        )


def test_models_forbid_extras_are_frozen_and_require_aware_times() -> None:
    record = _record()
    with pytest.raises(ValidationError):
        record.target_generation = 2
    with pytest.raises(ValidationError):
        ModelDeliveryPathRecord.model_validate({"extra": True})
    with pytest.raises(ValidationError):
        ModelDeliveryMerge(merge_commit="M1", merged_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValidationError):
        ModelDeliveryReference.model_validate({"ref_kind": "dev_merge_tag"})
    with pytest.raises(ValidationError, match="merge"):
        ModelDeliveryTag.model_validate({"tag": "T1", "artifact_identity": "sha-1"})
    assert (
        ModelDeliveryPathRecord.model_validate_json(record.model_dump_json()) == record
    )


@pytest.mark.parametrize("state", ACTIVE_STATES)
def test_path_retired_reaches_retired_from_every_active_state(state: str) -> None:
    handler = HandlerDeliveryPath()
    result = _fold(handler, _record(state), "path_retired", NOW)
    assert result.state == "retired"
    assert result.moved
    assert result.accepted
    assert not result.decision_rows
    assert result.record.target_tag == "T1"


def test_nothing_leaves_retired() -> None:
    handler = HandlerDeliveryPath()
    record = _record("retired")
    triggers = {t["trigger"] for t in _contract()["state_machine"]["transitions"]} | {
        _config().limit_trigger,
        None,
    }
    for trigger in triggers:
        result = _fold(handler, record, trigger, _at(1000))
        assert result.state == "retired"
        assert not result.moved
        assert not result.accepted
        assert not result.decision_rows
