# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19362 — the delegated test loop orchestrator, driven by fake children."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.handler_delegated_test_loop_orchestrator import (
    MAX_HOST_BUSY_TRIES,
    HandlerDelegatedTestLoopOrchestrator,
    result_json_bytes,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.handlers.replay import (
    replay_loop_receipt,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.models.model_delegated_test_loop import (
    MAX_RESULT_BYTES,
    EnumLoopStatus,
    ModelControlVerdict,
    ModelDelegatedTestLoopRequest,
    ModelDelegateReply,
    ModelLoopMutation,
    ModelRunDigest,
)
from omnimarket.nodes.node_delegated_test_loop_orchestrator.protocols.protocol_delegated_test_loop_ports import (
    LoopReceiptExistsError,
    ModelPrompt,
    ModelRunReceipt,
    ProtocolDelegatedTestLoopPorts,
)

pytestmark = pytest.mark.unit

FIXED = "4c3985a451847571e2cebd06a6edf1940069946e"
PREFIX = "01c504ceb492424cbcff59e4935b2912fd06a0ff"
CORRELATION = "0f1e2d3c-4b5a-4968-8778-a1b2c3d4e5f6"


def _digest(outcome: str, fingerprint: str = "", **extra: str) -> ModelRunDigest:
    return ModelRunDigest(
        receipt_id="",
        receipt_status="completed",
        outcome=outcome,  # type: ignore[arg-type]
        fingerprint=fingerprint,
        **extra,
    )


class FakePorts:
    """Scripted children. ``runs`` maps a ref role to the outcomes it returns in order."""

    def __init__(
        self,
        runs: dict[str, list[str]],
        *,
        fingerprints: list[str] | None = None,
        replies: list[ModelDelegateReply] | None = None,
        busy_first: int = 0,
        receipt_status: str = "completed",
    ) -> None:
        self.runs = {k: list(v) for k, v in runs.items()}
        self.fingerprints = list(fingerprints or [])
        self.replies = list(replies or [])
        self.busy_first = busy_first
        self.receipt_status = receipt_status
        self.delegate_calls = 0
        self.run_calls: list[tuple[str, str]] = []
        self.written: dict[str, dict[str, object]] = {}
        self.claimed: set[str] = set()
        self.last_seen: list[ModelRunDigest | None] = []

    def read_target(self, repo: str, ref: str, path: str) -> str:
        return "class KafkaSnapshotDeltaPublisher: ...\n"

    def build_prompt(self, request, target_excerpt, previous_test, last):  # type: ignore[no-untyped-def]
        self.last_seen.append(last)
        return ModelPrompt(prompt="p", response_contract={"type": "object"})

    def delegate(self, prompt: ModelPrompt, attempt: int) -> ModelDelegateReply:
        self.delegate_calls += 1
        if self.replies:
            return self.replies.pop(0)
        return ModelDelegateReply(
            run_id=f"run-{attempt}",
            ok=True,
            test_source=f"def test_x():\n    pass  # {attempt}\n",
            tokens_in=100,
            tokens_out=50,
            model="local",
        )

    def run(
        self,
        request,
        ref,
        ref_role: Literal["fixed", "prefix", "mutation"],
        attempt,
        test_source,
    ):  # type: ignore[no-untyped-def]
        self.run_calls.append((ref_role, ref))
        if self.busy_first:
            self.busy_first -= 1
            return ModelRunReceipt(
                receipt_id="", status="host_busy", exit_code=None, junit_xml=""
            )
        return ModelRunReceipt(
            receipt_id=f"{ref_role}-a{attempt}-{len(self.run_calls)}",
            status=self.receipt_status,  # type: ignore[arg-type]
            exit_code=0,
            junit_xml="<testsuites/>",
            detail="",
        )

    def digest(self, receipt: ModelRunReceipt) -> ModelRunDigest:
        role = receipt.receipt_id.split("-", 1)[0]
        outcome = self.runs[role].pop(0)
        fingerprint = (
            self.fingerprints.pop(0)
            if (outcome != "passed" and self.fingerprints)
            else ("" if outcome == "passed" else f"fp-{len(self.run_calls)}")
        )
        return _digest(outcome, fingerprint, message="assert x", top_frame="t.py:1")

    def grade(self, prefix_outcome, mutation_outcome, mutation_requested, same_ref):  # type: ignore[no-untyped-def]
        # A faithful copy of the control compute's table, enough for sequencing.
        if same_ref:
            status = (
                "control_did_not_fail" if prefix_outcome == "passed" else "infra_error"
            )
        elif prefix_outcome in ("passed", "no_tests"):
            status = "control_did_not_fail"
        elif prefix_outcome == "failed_call":
            status = "accepted_call"
        elif not mutation_requested:
            status = "accepted_collection"
        elif mutation_outcome is None:
            status = "needs_mutation_control"
        elif mutation_outcome == "failed_call":
            status = "accepted_mutation"
        else:
            status = "accepted_collection"
        return ModelControlVerdict(
            status=status,
            headline=status in ("accepted_call", "accepted_mutation"),
            control_ref_role="mutation" if mutation_outcome is not None else "prefix",
            control_outcome=mutation_outcome or prefix_outcome,
        )

    def wait_for_host(self, tries: int) -> None:
        return None

    def claim_loop_receipt(self, loop_run_id: str) -> None:
        if loop_run_id in self.claimed:
            raise LoopReceiptExistsError(loop_run_id)
        self.claimed.add(loop_run_id)

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        self.written[loop_run_id] = json.loads(json.dumps(payload))


def _request(**overrides: object) -> ModelDelegatedTestLoopRequest:
    values: dict[str, object] = {
        "correlation_id": CORRELATION,
        "repo": "OmniNode-ai/omnimarket",
        "fixed_ref": FIXED,
        "prefix_ref": PREFIX,
        "criterion": "publish inside a running loop refuses with a typed error",
        "target_path": "src/omnimarket/projection/snapshot_publisher.py",
        "test_path": "tests/unit/projection/test_dtl_generated_0f1e2d3c.py",
    }
    values.update(overrides)
    return ModelDelegatedTestLoopRequest(**values)  # type: ignore[arg-type]


def _run(ports: FakePorts, **overrides: object):  # type: ignore[no-untyped-def]
    assert isinstance(ports, ProtocolDelegatedTestLoopPorts)
    return HandlerDelegatedTestLoopOrchestrator(ports).run(_request(**overrides))


def test_attempt_bound_is_three() -> None:
    ports = FakePorts({"fixed": ["failed_call"] * 5})
    result = _run(ports)
    assert ports.delegate_calls == 3
    assert result.status is EnumLoopStatus.FAILED
    assert result.attempts == 3
    assert result.final_digest is not None


def test_two_identical_fingerprints_in_a_row_stop_with_no_progress() -> None:
    ports = FakePorts(
        {"fixed": ["failed_call"] * 3}, fingerprints=["same", "same", "x"]
    )
    result = _run(ports)
    assert result.status is EnumLoopStatus.NO_PROGRESS
    assert ports.delegate_calls == 2


def test_the_repair_call_sees_the_last_digest() -> None:
    ports = FakePorts({"fixed": ["failed_call", "passed"], "prefix": ["failed_call"]})
    _run(ports)
    assert ports.last_seen[0] is None
    assert ports.last_seen[1] is not None
    assert ports.last_seen[1].outcome == "failed_call"


def test_a_call_phase_control_failure_is_accepted_call() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})
    result = _run(ports)
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.headline is True
    assert result.control is not None
    assert (result.control.ref, result.control.ref_role) == (PREFIX, "prefix")
    assert ports.run_calls == [("fixed", FIXED), ("prefix", PREFIX)]


def test_a_collection_control_with_a_mutation_runs_the_mutation_at_the_fixed_ref() -> (
    None
):
    ports = FakePorts(
        {
            "fixed": ["passed"],
            "prefix": ["failed_collection"],
            "mutation": ["failed_call"],
        }
    )
    result = _run(
        ports, mutations=(ModelLoopMutation(path="src/a.py", find="x", replace="y"),)
    )
    assert result.status is EnumLoopStatus.ACCEPTED_MUTATION
    assert result.headline is True
    assert ports.run_calls[-1] == ("mutation", FIXED)
    assert result.control is not None
    assert result.control.prefix_outcome == "failed_collection"


def test_a_collection_control_without_a_mutation_is_accepted_weak() -> None:
    result = _run(FakePorts({"fixed": ["passed"], "prefix": ["failed_collection"]}))
    assert result.status is EnumLoopStatus.ACCEPTED_COLLECTION
    assert result.headline is False


def test_the_negative_control_never_accepts() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["passed"]})
    result = _run(ports, prefix_ref=FIXED)
    assert result.status is EnumLoopStatus.CONTROL_DID_NOT_FAIL
    assert ports.run_calls == [("fixed", FIXED), ("prefix", FIXED)]


@pytest.mark.parametrize("where", ["fixed", "prefix"])
def test_infra_error_is_never_accepted(where: str) -> None:
    runs = {"fixed": ["passed"], "prefix": ["failed_call"]}
    runs[where] = ["infra_error", *runs[where][1:]]
    result = _run(FakePorts(runs))
    assert result.status is EnumLoopStatus.INFRA_ERROR
    assert result.headline is False


def test_an_infra_error_receipt_is_never_accepted() -> None:
    result = _run(FakePorts({"fixed": ["passed"]}, receipt_status="infra_error"))
    assert result.status is EnumLoopStatus.INFRA_ERROR


def test_a_busy_host_is_retried_then_host_busy() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]}, busy_first=2)
    assert _run(ports).status is EnumLoopStatus.ACCEPTED_CALL
    stuck = FakePorts({"fixed": ["passed"]}, busy_first=MAX_HOST_BUSY_TRIES)
    assert _run(stuck).status is EnumLoopStatus.HOST_BUSY


def test_an_unusable_reply_counts_as_an_attempt_and_is_fed_back() -> None:
    bad = ModelDelegateReply(run_id="run-1", ok=False, invalid_reason="not JSON")
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]}, replies=[bad])
    result = _run(ports)
    assert result.status is EnumLoopStatus.ACCEPTED_CALL
    assert result.attempts == 2
    assert ports.last_seen[1] is not None
    assert "not JSON" in ports.last_seen[1].message


def test_a_rerun_of_the_same_correlation_is_refused() -> None:
    ports = FakePorts({"fixed": ["passed"], "prefix": ["failed_call"]})
    handler = HandlerDelegatedTestLoopOrchestrator(ports)
    handler.run(_request())
    with pytest.raises(LoopReceiptExistsError):
        handler.run(_request())


def test_the_result_carries_no_test_source_and_stays_under_4kb() -> None:
    long_message = "m" * 500
    ports = FakePorts({"fixed": ["failed_call"] * 3})
    ports.digest = lambda receipt: _digest(  # type: ignore[method-assign]
        "failed_call",
        "fp" + receipt.receipt_id,
        message=long_message,
        exception_type="E" * 200,
        top_frame="t" * 400,
        frames="f" * 1500,
    )
    result = _run(ports)
    assert result_json_bytes(result) < MAX_RESULT_BYTES
    assert "def test_x" not in json.dumps(result.model_dump(mode="json"))


def test_the_loop_receipt_lists_every_child_and_replays_to_the_same_status() -> None:
    ports = FakePorts(
        {
            "fixed": ["failed_call", "passed"],
            "prefix": ["failed_collection"],
            "mutation": ["failed_call"],
        },
        busy_first=1,
    )
    result = _run(
        ports, mutations=(ModelLoopMutation(path="src/a.py", find="x", replace="y"),)
    )
    receipt = ports.written[CORRELATION]
    assert receipt["delegate_run_ids"] == ["run-1", "run-2"]
    assert receipt["run_receipt_ids"] == list(result.run_receipt_ids)
    assert len(result.run_receipt_ids) == 4  # fixed x2, prefix, mutation
    assert (
        replay_loop_receipt(receipt)
        is result.status
        is EnumLoopStatus.ACCEPTED_MUTATION
    )


def test_an_unbound_handler_refuses_to_run() -> None:
    with pytest.raises(RuntimeError, match="no loop ports"):
        HandlerDelegatedTestLoopOrchestrator().run(_request())


def test_the_node_declares_no_plugin_class_and_no_envelope() -> None:
    node_dir = (
        Path(__file__).parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegated_test_loop_orchestrator"
    )
    sources = [p.read_text() for p in node_dir.rglob("*.py")]
    assert sources
    assert not [s for s in sources if "class Plugin" in s]
    assert not [s for s in sources if "ModelEventEnvelope" in s]
