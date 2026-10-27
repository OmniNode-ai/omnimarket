# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Acceptance falsifiers for manifest-backed attempt receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import ValidationError

from omnimarket.nodes.node_model_work_receipt_join_compute import (
    HandlerModelWorkReceiptJoin,
    ModelAttemptManifestRow,
    ModelCheckExecution,
    ModelContractCheck,
    ModelReceiptJoinRequest,
    ModelSourceReceipt,
    ModelWorkAttemptReceipt,
    ModelWorkUnjoinedReceipt,
)

FIXTURES = Path(__file__).parents[1] / "fixtures/model_work_receipts"
OUTPUT = '{"answer":42}'
DIGEST = hashlib.sha256(OUTPUT.encode()).hexdigest()
CHECK = "a" * 64


def manifest(**changes):
    return ModelAttemptManifestRow.model_validate(
        dict(
            contract_id="contract-1",
            attempt_id="attempt-1",
            arm="codex",
            draw=1,
            correlation_ids=["11111111-1111-4111-8111-111111111111"],
            landing_lease_ids=["lease-1"],
            **changes,
        )
    )


def source(family="remote_lane", ref="remote.json", **data):
    return ModelSourceReceipt(family=family, source_ref=ref, document=data)


def remote(**data):
    return source(
        contract_line="Contract: contract-1",
        attempt_line="Attempt: attempt-1",
        model="codex",
        engine="codex",
        **data,
    )


def request(*sources, executions=None, contracts=None, **changes):
    return ModelReceiptJoinRequest(
        manifest=(manifest(),),
        sources=sources,
        contracts=contracts
        if contracts is not None
        else (
            ModelContractCheck(
                contract_id="contract-1", check_sha256=CHECK, expected_exit=0
            ),
        ),
        checks=executions
        if executions is not None
        else (
            ModelCheckExecution(
                contract_id="contract-1",
                attempt_id="attempt-1",
                check_sha256=CHECK,
                output_sha256=DIGEST,
                exit_code=0,
            ),
        ),
        outputs={"attempt-1": OUTPUT},
        **changes,
    )


def join(*sources, **changes):
    return HandlerModelWorkReceiptJoin().handle(request(*sources, **changes))


def test_absent_is_unknown():
    row = join(remote()).receipts[0]
    for key in (
        "tokens_in",
        "tokens_out",
        "cache_read",
        "cache_write",
        "wall_s",
        "turns",
        "tool_calls",
        "retries",
        "cost_usd",
    ):
        assert getattr(row, key) == "UNKNOWN", key
    assert row.effort == "UNKNOWN"
    assert row.rejection_reasons == "UNKNOWN"
    assert row.cost_basis == "UNKNOWN"
    with pytest.raises(ValidationError):
        row.turns = 0
    with pytest.raises(ValidationError):
        ModelWorkAttemptReceipt.model_validate({**row.model_dump(), "turns": -1})
    assert join(remote(turns=0, tool_calls=0)).receipts[0].tool_calls == 0


def test_codex_zero_cost_is_unknown():
    for name, expected in (("codex", "UNKNOWN"), ("sonnet", "MEASURED")):
        data = json.loads((FIXTURES / f"{name}_remote.json").read_text())
        data.update(
            contract_line="Contract: contract-1", attempt_line="Attempt: attempt-1"
        )
        row = join(source(**data)).receipts[0]
        assert row.cost_basis == expected
        assert row.cost_usd == "UNKNOWN" if name == "codex" else row.cost_usd > 0
        # Legacy num_turns counts stream items for Codex, never assistant messages.
        if name == "codex":
            assert row.turns == "UNKNOWN"


def test_rerun_idempotent():
    first = join(remote(tokens_in=20))
    second = join(
        remote(tokens_in=20),
        existing_receipts=first.receipts,
        existing_unjoined=first.unjoined,
    )
    assert first == second
    assert len(second.receipts) == 1
    duplicate = join(remote(tokens_in=20), remote(tokens_in=20))
    assert duplicate == first


def test_unjoined_counted():
    result = join(source(model="codex", total_cost_usd=0))
    assert not result.receipts
    assert result.summary.unjoined == 1
    assert result.summary.per_pass_eligible == 0
    assert isinstance(result.unjoined[0], ModelWorkUnjoinedReceipt)
    assert result.unjoined[0].verdict == "unjoined"
    assert result == join(
        source(model="codex", total_cost_usd=0), existing_unjoined=result.unjoined
    )


def test_verdict_from_check():
    failed = ModelCheckExecution(
        contract_id="contract-1",
        attempt_id="attempt-1",
        check_sha256=CHECK,
        output_sha256=DIGEST,
        exit_code=1,
    )
    assert (
        join(remote(verdict="pass", status="done"), executions=(failed,))
        .receipts[0]
        .verdict
        == "fail"
    )
    assert join(remote(verdict="fail")).receipts[0].verdict == "pass"
    tampered = failed.model_copy(update={"output_sha256": "b" * 64})
    assert (
        join(remote(), executions=(tampered,)).unjoined[0].reason
        == "check_output_mismatch"
    )
    assert join(remote(), executions=()).unjoined[0].reason == "missing_check"
    assert join(remote(), contracts=()).unjoined[0].reason == "missing_contract_check"


def test_attempt_identity_and_conflicts():
    result = join(
        remote(tokens_in=20, wall_s=4),
        source(
            "delegate",
            "delegate.json",
            correlation_id="11111111-1111-4111-8111-111111111111",
            metrics={"input_tokens": 21, "output_tokens": 5},
            attempts=[
                {
                    "acceptance_reason": "rubric rejection",
                    "acceptance_decision": "reject",
                }
            ],
        ),
        source(
            "landing",
            "cost.jsonl:1",
            lease_id="lease-1",
            total_cost_usd=900,
            duration_ms=4100,
            usage_source="MEASURED",
        ),
        source(
            "harness_delegate",
            "old.json",
            correlation_id="22222222-2222-4222-8222-222222222222",
            usage={"input_tokens": 3},
        ),
    )
    (row,) = result.receipts
    assert (row.contract_id, row.attempt_id, row.arm, row.draw) == (
        "contract-1",
        "attempt-1",
        "codex",
        1,
    )
    assert row.cost_usd == "UNKNOWN"
    assert row.tokens_in == "UNKNOWN"
    assert row.tokens_out == 5
    assert row.rejection_reasons == ("rubric rejection",)
    assert result.conflicts[0].field == "tokens_in"
    assert result.conflicts[0].source_refs == ("remote.json", "delegate.json")
    assert result.summary.unjoined == 1
    assert result.summary.per_pass_eligible == 0
    assert result.landing_cross_checks[0].wall_s == Decimal("4.1")
    assert "cost.jsonl:1" not in row.source_refs
    # A matching textual id without the pre-dispatch manifest is insufficient.
    empty = request(remote()).model_copy(update={"manifest": ()})
    assert HandlerModelWorkReceiptJoin().handle(empty).summary.unjoined == 1


def test_all_families_and_counter_semantics():
    data = json.loads((FIXTURES / "harness.json").read_text())
    data["correlation_id"] = "11111111-1111-4111-8111-111111111111"
    row = join(source("harness_delegate", "harness.json", **data)).receipts[0]
    assert (row.tokens_in, row.tokens_out, row.turns, row.tool_calls) == (
        4416,
        382,
        1,
        0,
    )
    assert row.cache_read == "UNKNOWN"
    delegate = source(
        "delegate",
        "delegate.json",
        correlation_id="11111111-1111-4111-8111-111111111111",
        metrics={
            "input_tokens": 30,
            "output_tokens": 2,
            "cost_usd": 0,
            "cost_measured": True,
        },
        attempts=[{"input_tokens_measured": None}],
    )
    row = join(delegate).receipts[0]
    assert row.tokens_in == 30
    assert row.cost_basis == "MEASURED"  # Explicitly measured free local inference.
    assert row.cost_usd == 0


async def test_inmemory_bus_binding():
    """Round-trip files' typed payload over a customer-owned in-memory bus."""
    bus = EventBusInmemory()
    await bus.start()
    topic = "test.model-work-receipt-join"
    expected = join(remote(tokens_in=20))
    await bus.publish(topic, None, expected.model_dump_json().encode())
    history = await bus.get_event_history()
    from omnimarket.nodes.node_model_work_receipt_join_compute import (
        ModelReceiptJoinResult,
    )

    assert ModelReceiptJoinResult.model_validate_json(history[0].value) == expected
    await bus.close()


def test_sqlite_writer_binding_rerun_idempotent(tmp_path):
    """The comparison run owns writes; use its replace-by-key semantics."""
    result = join(remote(tokens_in=20))
    with sqlite3.connect(tmp_path / "attempts.db") as db:
        db.execute(
            "CREATE TABLE attempts (contract_id TEXT, attempt_id TEXT, row TEXT, PRIMARY KEY(contract_id, attempt_id))"
        )
        for _ in range(2):
            for row in result.receipts:
                db.execute(
                    "INSERT OR REPLACE INTO attempts VALUES (?, ?, ?)",
                    (row.contract_id, row.attempt_id, row.model_dump_json()),
                )
        rows = db.execute("SELECT row FROM attempts").fetchall()
    assert len(rows) == 1
    assert ModelWorkAttemptReceipt.model_validate_json(rows[0][0]) == result.receipts[0]


def test_ambiguous_delegate_attempts_are_not_aggregated():
    result = join(
        source(
            "delegate",
            "multi.json",
            correlation_id="11111111-1111-4111-8111-111111111111",
            attempts=[{"cost_usd": 1}, {"cost_usd": 2}],
            metrics={"input_tokens": 100, "cost_usd": 3, "cost_measured": True},
        )
    )
    assert not result.receipts
    assert result.unjoined[0].reason == "ambiguous_delegate_attempts"
    assert result.summary.per_pass_eligible == 0


def test_identity_conflicts_do_not_relabel_a_manifest_attempt():
    result = join(
        source(
            contract_line="Contract: another-contract",
            attempt_line="Attempt: attempt-1",
        )
    )
    assert result.unjoined[0].reason == "identity_conflict"
    assert not result.receipts
    with pytest.raises(ValidationError, match="Ambiguous"):
        request(remote()).model_copy().model_validate(
            {
                **request(remote()).model_dump(),
                "manifest": [manifest().model_dump(), manifest().model_dump()],
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("turns", -1),
        ("tokens_in", True),
        ("cost_usd", "NaN"),
        ("wall_s", "Infinity"),
        ("tool_calls", 1.5),
    ],
)
def test_invalid_measurements_fail_closed(field, value):
    with pytest.raises(ValidationError):
        join(remote(**{field: value}))


def test_verdict_from_check_rejects_an_unpinned_checker():
    check = ModelCheckExecution(
        contract_id="contract-1",
        attempt_id="attempt-1",
        check_sha256="f" * 64,
        output_sha256=DIGEST,
        exit_code=0,
    )
    result = join(remote(verdict="pass"), executions=(check,))
    assert not result.receipts
    assert result.unjoined[0].reason == "check_spec_mismatch"


def test_manifest_uuid_survives_and_nested_delegate_metrics_join():
    from uuid import UUID

    assert isinstance(manifest().correlation_ids[0], UUID)
    row = join(
        source(
            "delegate",
            "nested.json",
            correlation_id="11111111-1111-4111-8111-111111111111",
            receipt={"result": {"metrics": {"input_tokens": 50, "output_tokens": 8}}},
        )
    ).receipts[0]
    assert (row.tokens_in, row.tokens_out) == (50, 8)


def test_per_pass_denominator_excludes_unknown_and_failed_attempts():
    assert (
        join(remote(tokens_in=2, tokens_out=3, wall_s=4)).summary.per_pass_eligible == 1
    )
    assert join(remote(tokens_in=2, wall_s=4)).summary.per_pass_eligible == 0
    check = ModelCheckExecution(
        contract_id="contract-1",
        attempt_id="attempt-1",
        check_sha256=CHECK,
        output_sha256=DIGEST,
        exit_code=1,
    )
    assert (
        join(
            remote(tokens_in=2, tokens_out=3, wall_s=4), executions=(check,)
        ).summary.per_pass_eligible
        == 0
    )


def test_order_independence():
    sources = (
        remote(tokens_in=20),
        source(
            "delegate",
            "delegate.json",
            correlation_id="11111111-1111-4111-8111-111111111111",
            metrics={"input_tokens": 21},
        ),
    )
    assert join(*sources) == join(*reversed(sources))


def test_verdict_from_check_real_lab_execution(tmp_path):
    """The lab runner executes the check; the compute node verifies its evidence."""
    import subprocess
    import sys

    output_path = tmp_path / "output.json"
    checker = "import json,sys; assert json.load(open(sys.argv[1]))['answer'] == 42"
    checker_hash = hashlib.sha256(checker.encode()).hexdigest()
    expectation = ModelContractCheck(
        contract_id="contract-1", check_sha256=checker_hash, expected_exit=0
    )
    for output, expected in ((OUTPUT, "pass"), ('{"answer":0}', "fail")):
        output_path.write_text(output)
        completed = subprocess.run(
            [sys.executable, "-c", checker, str(output_path)],
            check=False,
            capture_output=True,
            timeout=10,
        )
        execution = ModelCheckExecution(
            contract_id="contract-1",
            attempt_id="attempt-1",
            check_sha256=checker_hash,
            output_sha256=hashlib.sha256(output.encode()).hexdigest(),
            exit_code=completed.returncode,
        )
        payload = request(
            remote(verdict="pass"), contracts=(expectation,), executions=(execution,)
        )
        payload = payload.model_copy(update={"outputs": {"attempt-1": output}})
        assert (
            HandlerModelWorkReceiptJoin().handle(payload).receipts[0].verdict
            == expected
        )


def test_deploy_registration_and_policy():
    import importlib
    from importlib.metadata import entry_points

    import yaml
    from omnibase_core.nodes.node_compute import NodeCompute

    from omnimarket.nodes.node_model_work_receipt_join_compute.models.model_work_attempt_receipt import (
        ModelReceiptJoinPolicy,
    )
    from omnimarket.nodes.node_model_work_receipt_join_compute.node import (
        NodeModelWorkReceiptJoinCompute,
    )

    root = Path(__file__).parents[2]
    node = root / "src/omnimarket/nodes/node_model_work_receipt_join_compute"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert cls is HandlerModelWorkReceiptJoin
    registered = list(entry_points(group="onex.nodes", name=node.name))
    assert len(registered) == 1
    assert registered[0].value == "omnimarket.nodes." + node.name
    assert issubclass(NodeModelWorkReceiptJoinCompute, NodeCompute)
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert (
        ModelReceiptJoinPolicy.model_validate(contract["configuration"])
        == ModelReceiptJoinPolicy()
    )
    overlay = yaml.safe_load((node / "overlay.yaml").read_text())
    enabled = overlay.pop("enabled")
    runtime_profiles = overlay.pop("runtime_profiles")
    overlay_node = overlay.pop("node")
    assert not enabled
    assert runtime_profiles == []
    assert overlay_node == node.name
    assert ModelReceiptJoinPolicy.model_validate(overlay) == ModelReceiptJoinPolicy()
    assert registered[0].load() is importlib.import_module(registered[0].value)


def test_canonical_source_identity_and_explicit_contract_conflict():
    left = source(model="codex", total_cost_usd=0)
    right = source(total_cost_usd=0, model="codex")
    assert join(left, right).summary.unjoined == 1
    result = join(remote(contract_id="another-contract"))
    assert result.unjoined[0].reason == "identity_conflict"
    assert not result.receipts


def test_existing_rows_cannot_bypass_the_manifest():
    row = join(remote()).receipts[0]
    data = request(remote(), existing_receipts=(row,)).model_dump()
    data["manifest"] = []
    with pytest.raises(ValidationError, match="pre-dispatch manifest"):
        ModelReceiptJoinRequest.model_validate(data)


def test_rejoin_replaces_a_prior_missing_check_disposition():
    previous = join(remote(), executions=())
    assert previous.summary.unjoined == 1
    result = join(remote(), existing_unjoined=previous.unjoined)
    assert result.summary.unjoined == 0
    assert len(result.receipts) == 1
    assert result.receipts[0].verdict == "pass"
