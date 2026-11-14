# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain (OMN-22958): the packaged contract resolves to the handler, a good request reads
WORKING with the topics the contract declares, and each named mutation turns the chain red.

Every mutation in NAMED_MUTATIONS replaces one rule in the handler module and runs the same golden
assertions; the mutated chain must raise AssertionError, which shows the assertion can fail.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReading,
    ModelDevDeployReadingRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict as V
from omnimarket.nodes.node_dev_deploy_reading_compute.handlers import (
    handler_dev_deploy_reading as module,
)
from tests.nodes.node_dev_deploy_reading_compute.dev_deploy_support import (
    SHA_A,
    SHA_B,
    SHA_C,
    contract,
    lineage_fact,
    merge,
    probe,
    receipt,
    request,
    resolve_handler,
    rule,
)

#: mutation name -> (the chain it turns red, the test that shows it).
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "pending-merge-counted-as-failure": (
        "golden",
        "test_mutation_pending_merge_counted_as_failure_turns_the_golden_chain_red",
    ),
    "probe-always-missing": (
        "golden",
        "test_mutation_probe_always_missing_turns_the_golden_chain_red",
    ),
    "receipts-ignored": (
        "golden",
        "test_mutation_receipts_ignored_turns_the_golden_chain_red",
    ),
    "covering-receipt-never": (
        "golden",
        "test_mutation_covering_receipt_never_turns_the_golden_chain_red",
    ),
    "probe-sha-always-mismatch": (
        "golden",
        "test_mutation_probe_sha_always_mismatch_turns_the_golden_chain_red",
    ),
}


def run_golden_chain() -> None:
    """The golden assertions: contract routing, the good request, a pending merge."""
    bus = contract()["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.dev-deploy-read-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.dev-deploy-read.v1"]
    assert contract()["terminal_event"] in bus["publish_topics"]
    handler = resolve_handler()()
    reading = handler.handle(request())
    assert isinstance(reading, ModelDevDeployReading)
    assert reading.verdict is V.WORKING
    assert reading.reasons == ()
    assert (reading.merges_checked, reading.merges_receipted) == (2, 2)
    pending = handler.handle(
        request(merges=[merge(SHA_A, "2026-11-08T11:30:00Z")], receipts=[])
    )
    assert pending.verdict is V.WORKING
    assert pending.pending == (f"receipt-pending:{SHA_A[:12]}",)
    assert pending.merges_receipted == 0


def test_contract_declares_a_pure_compute_node_with_one_operation() -> None:
    declared = contract()
    assert declared["name"] == "node_dev_deploy_reading_compute"
    assert declared["node_type"] == "compute"
    assert declared["descriptor"]["side_effects"] == []
    [entry] = declared["handler_routing"]["handlers"]
    assert declared["handler_routing"]["routing_strategy"] == "operation_match"
    assert entry["operation"] == "dev_deploy_reading"
    assert resolve_handler() is module.HandlerDevDeployReading
    assert (
        declared["handler"]["input_model"].rpartition(".")[2]
        == ModelDevDeployReadingRequest.__name__
    )


def test_golden_chain_good_request_reads_working_through_the_contract_handler() -> None:
    run_golden_chain()


def _pending_as_failure(real: Callable[..., Any]) -> Callable[..., Any]:
    def mutated(req: ModelDevDeployReadingRequest) -> Any:
        failures, pending, checked, receipted = real(req)
        return failures + pending, [], checked, receipted

    return mutated


def test_mutation_pending_merge_counted_as_failure_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module, "judge_receipts", _pending_as_failure(module.judge_receipts)
    )
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_probe_always_missing_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "judge_probe", lambda _req: (["probe-missing"], None))
    with pytest.raises(AssertionError):
        run_golden_chain()


def test_mutation_receipts_ignored_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "judge_receipts", lambda _req: ([], [], 0, 0))
    with pytest.raises(AssertionError):
        run_golden_chain()


def run_lineage_golden_chain() -> None:
    """The lineage rule (OMN-23603): a later PASS receipt covers the merge, a descendant running sha passes."""
    handler = resolve_handler()()
    covered = handler.handle(
        request(
            merges=[merge(SHA_A, "2026-11-08T08:00:00Z")],
            receipts=[receipt(SHA_B, "2026-11-08T08:20:00Z")],
            lineage_rule=rule(receipt_covers_descendant=True),
            lineage=[lineage_fact(SHA_A, SHA_B)],
        )
    )
    assert covered.verdict is V.WORKING
    accepted = handler.handle(
        request(
            probes=[probe(expected=SHA_B, observed=SHA_C)],
            lineage_rule=rule(probe_accepts_descendant=True),
            lineage=[lineage_fact(SHA_B, SHA_C)],
        )
    )
    assert accepted.verdict is V.WORKING


def test_lineage_golden_chain_reads_working() -> None:
    run_lineage_golden_chain()


def test_mutation_covering_receipt_never_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_covering_receipt", lambda *_a, **_k: (False, None))
    with pytest.raises(AssertionError):
        run_lineage_golden_chain()


def test_mutation_probe_sha_always_mismatch_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        module,
        "_probe_sha_failures",
        lambda _req, probe, _observed: [
            f"probe-sha-mismatch:{probe.probe_id}:{probe.expected_sha[:12]}"
        ],
    )
    with pytest.raises(AssertionError):
        run_lineage_golden_chain()


def test_the_golden_chain_is_green_again_after_each_mutation_is_undone() -> None:
    """Positive control: the chain the mutations break is the one that passes unmutated."""
    run_golden_chain()
    run_lineage_golden_chain()


def test_every_named_golden_mutation_has_its_test() -> None:
    names = {n for n, v in globals().items() if n.startswith("test_") and callable(v)}
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden"}
