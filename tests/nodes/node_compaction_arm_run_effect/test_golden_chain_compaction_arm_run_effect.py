# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_compaction_arm_run_effect.

The chain is: command topic -> HandlerCompactionArmRunRuntime -> per-arm
adapters -> terminal topic, with the native fork leg answered by
node_llm_delegation_call_effect over its declared request and reply topics.
Every topic below is asserted against the contract that declares it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from omnimarket.models.compaction_replay.model_transcript_manifest import (
    ModelTranscriptCase,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run_runtime import (
    HandlerCompactionArmRunRuntime,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelArmRunRequest,
    ModelCompactionObservation,
    ModelPremiseEvidence,
    ModelUsage,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_runtime_binding import (
    ModelArmRuntimeBinding,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _contract(node: str) -> dict:
    return yaml.safe_load((NODES / node / "contract.yaml").read_text())


class _Adapter:
    def __init__(self, *, native: bool) -> None:
        self.native = native

    def compact(self, request, workspace):
        return ModelCompactionObservation(
            reply="native" if self.native else "installed",
            context="native summary" if self.native else "mod package",
            usage=ModelUsage(input_tokens=12, output_tokens=4, measurement="measured"),
            evidence="golden chain adapter",
        )


def _request() -> ModelArmRunRequest:
    case = ModelTranscriptCase(
        case_key="golden-chain",
        session_id="session-one",
        boundary_uuid="boundary-one",
        transcript_path="session.jsonl",
        trigger="manual",
        pre_tokens=12,
        post_tokens=4,
        boundary_at="2026-10-27T12:00:00Z",
        boundary_sha256=_sha(b"boundary"),
        summary_record_sha256=_sha(b"record"),
        summary_length=len("recorded summary"),
        summary_sha256=_sha(b"recorded summary"),
        summary_check="PINNED",
        prefix_bytes=len(b"frozen transcript\n"),
        prefix_sha256=_sha(b"frozen transcript\n"),
        prefix_records=1,
        ledger_cuts=(
            {
                "name": "ledger.md",
                "prefix_bytes": len(b"frozen ledger\n"),
                "prefix_sha256": _sha(b"frozen ledger\n"),
                "prefix_rows": 1,
            },
        ),
        lane_attribution={"lanes": ("example",), "rule": "fixture", "evidence": ()},
    )
    return ModelArmRunRequest(
        case=case,
        transcript_prefix=b"frozen transcript\n",
        ledger_files=({"name": "ledger.md", "content": b"frozen ledger\n"},),
        recorded_summary="recorded summary",
        arms=("stock_rerun", "mod", "stock_recorded"),
        premise=ModelPremiseEvidence(
            manual_compaction=True,
            transcript_retained=True,
            evidence="headless boundary observed",
        ),
    )


def test_arm_node_topic_chain_is_declared_end_to_end() -> None:
    arm = _contract("node_compaction_arm_run_effect")
    delegation = _contract("node_llm_delegation_call_effect")

    assert arm["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omniintelligence.compaction-arm-run-requested.v1"
    ]
    assert set(arm["event_bus"]["publish_topics"]) == {
        "onex.cmd.omniintelligence.compaction-arm-run-completed.v1",
        "onex.cmd.omniintelligence.context-pack-requested.v1",
        "onex.cmd.omniintelligence.native-compaction-fork-requested.v1",
    }
    assert (
        arm["terminal_event"]
        == "onex.cmd.omniintelligence.compaction-arm-run-completed.v1"
    )
    assert (
        "onex.cmd.omniintelligence.native-compaction-fork-requested.v1"
        in delegation["event_bus"]["subscribe_topics"]
    )
    assert (
        "onex.cmd.omniintelligence.native-compaction-fork-completed.v1"
        in delegation["event_bus"]["publish_topics"]
    )
    assert (
        arm["arm_runtime"]["native_completed_topic"]
        == "onex.cmd.omniintelligence.native-compaction-fork-completed.v1"
    )


def test_native_reply_topic_leaves_the_graph_as_declared_request_reply() -> None:
    delegation = _contract("node_llm_delegation_call_effect")
    arm = _contract("node_compaction_arm_run_effect")

    reply = next(
        i
        for i in arm["event_bus"]["request_response"]["instances"]
        if i["name"] == "compaction_arm_native_fork"
    )
    assert (
        reply["reply_topics"]["completed"]
        == "onex.cmd.omniintelligence.native-compaction-fork-completed.v1"
    )
    assert (
        "onex.cmd.omniintelligence.native-compaction-fork-completed.v1"
        in delegation["externally_consumed_topics"]
    )


def test_runtime_handler_produces_one_record_per_requested_arm(
    tmp_path: Path,
) -> None:
    settings = _contract("node_compaction_arm_run_effect")["arm_runtime"]
    binding = ModelArmRuntimeBinding(
        workspace_root=tmp_path / "node-runs",
        native_timeout_s=1,
        pack_timeout_s=1,
        profile_name=settings["profile_name"],
        tenant_id="fixture",
        pack_requested_topic=settings["pack_requested_topic"],
        pack_built_topic=settings["pack_built_topic"],
        pack_failed_topic=settings["pack_failed_topic"],
        native_requested_topic=settings["native_requested_topic"],
        native_completed_topic=settings["native_completed_topic"],
    )
    runtime = HandlerCompactionArmRunRuntime(
        binding=binding, stock=_Adapter(native=True), mod=_Adapter(native=False)
    )
    request = _request()

    result = runtime.handle(request)

    assert [r.arm for r in result.records] == ["stock_rerun", "mod", "stock_recorded"]
    assert all(r.scorable for r in result.records)
    assert len({r.initial_state_hash for r in result.records}) == 1
    assert result.correlation_id == request.correlation_id
