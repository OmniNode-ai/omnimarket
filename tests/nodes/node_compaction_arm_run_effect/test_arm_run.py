# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Arm producer falsifiers: frozen inputs, isolation, refusals and fallback accounting."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omnimarket.models.compaction_replay.model_transcript_manifest import (
    ModelTranscriptCase,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run import (
    HandlerCompactionArmRun,
)
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.workspace import (
    HandlerForkWorkspace,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    ModelArmRunRequest,
    ModelCompactionObservation,
    ModelPremiseEvidence,
    ModelUsage,
)

pytestmark = pytest.mark.unit


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def case() -> ModelTranscriptCase:
    return ModelTranscriptCase(
        case_key="case-one",
        session_id="session-one",
        boundary_uuid="boundary-one",
        transcript_path="session.jsonl",
        trigger="manual",
        pre_tokens=12,
        post_tokens=4,
        boundary_at="2026-10-27T12:00:00Z",
        boundary_sha256=sha(b"boundary"),
        summary_record_sha256=sha(b"record"),
        summary_length=len("recorded summary"),
        summary_sha256=sha(b"recorded summary"),
        summary_check="PINNED",
        prefix_bytes=len(b"frozen transcript\n"),
        prefix_sha256=sha(b"frozen transcript\n"),
        prefix_records=1,
        ledger_cuts=(
            {
                "name": "ledger.md",
                "prefix_bytes": len(b"frozen ledger\n"),
                "prefix_sha256": sha(b"frozen ledger\n"),
                "prefix_rows": 1,
            },
        ),
        lane_attribution={"lanes": ("example",), "rule": "fixture", "evidence": ()},
    )


@pytest.fixture
def arm_request(case) -> ModelArmRunRequest:
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


class ScriptedAdapter:
    def __init__(self, *, native=False, custom=False, mutate=False):
        self.native, self.custom, self.mutate = native, custom, mutate
        self.calls = []

    def compact(self, arm_request, workspace):
        self.calls.append(workspace)
        if self.mutate:
            workspace.transcript.write_bytes(b"changed prefix")
        return ModelCompactionObservation(
            reply="native" if self.native else "installed",
            context="native summary" if self.native else "mod package",
            custom_boundary_evidence=("custom-boundary",) if self.custom else (),
            usage=ModelUsage(input_tokens=12, output_tokens=4, measurement="measured"),
            evidence="scripted adapter",
        )


def handler(tmp_path, stock=None, mod=None):
    return HandlerCompactionArmRun(
        workspace=HandlerForkWorkspace(tmp_path / "runs"),
        stock=stock or ScriptedAdapter(native=True),
        mod=mod or ScriptedAdapter(),
    )


def test_two_forks_equal_initial_state_and_isolated_worktrees(tmp_path, arm_request):
    forks = HandlerForkWorkspace(tmp_path / "runs")
    left = forks.fork(arm_request, "stock_rerun", 0)
    right = forks.fork(arm_request, "mod", 0)
    assert left.initial_state_hash == right.initial_state_hash
    assert left.prefix_hash == right.prefix_hash == arm_request.case.prefix_sha256
    assert left.ledger_hash == right.ledger_hash
    left.ledger_files[0].write_bytes(b"private write")
    assert right.ledger_files[0].read_bytes() == b"frozen ledger\n"
    assert arm_request.ledger_files[0].content == b"frozen ledger\n"


def test_mod_native_reply_counts_in_fallback_rate(tmp_path, arm_request):
    result = handler(tmp_path, mod=ScriptedAdapter(native=True)).handle(arm_request)
    assert result.mod_attempts == result.mod_fallbacks == 1
    assert result.mod_fallback_rate == 1.0
    assert next(r for r in result.records if r.arm == "mod").native_fallback


def test_custom_boundary_stock_is_invalid(tmp_path, arm_request):
    result = handler(tmp_path, stock=ScriptedAdapter(native=True, custom=True)).handle(
        arm_request
    )
    stock = next(r for r in result.records if r.arm == "stock_rerun")
    assert stock.status == "INVALID"
    assert stock.context is None
    assert not stock.scorable


@pytest.mark.parametrize(
    ("manual", "retained"), [(False, True), (True, False), (False, False)]
)
def test_refused_premise_is_visible_and_recorded_stock_used(
    tmp_path, arm_request, manual, retained
):
    stock = ScriptedAdapter(native=True)
    arm_request = arm_request.model_copy(
        update={
            "premise": ModelPremiseEvidence(
                manual_compaction=manual,
                transcript_retained=retained,
                evidence="headless probe did not satisfy the premise",
            )
        }
    )
    result = handler(tmp_path, stock=stock).handle(arm_request)
    refusal = next(r for r in result.records if r.arm == "stock_rerun")
    assert refusal.status == "REFUSED"
    assert not refusal.scorable
    assert refusal.reason == arm_request.premise.evidence
    assert result.stock_comparison == "stock_recorded"
    assert result.comparison_label == "RECORDED_STOCK"
    assert result.planted_safeguard == "NOT_ESTABLISHED"
    assert not stock.calls
    assert len([r for r in result.records if r.arm == "stock_recorded"]) == 1


def test_changed_prefix_or_ledger_refused_before_adapter(tmp_path, arm_request):
    adapter = ScriptedAdapter()
    bad = arm_request.model_copy(update={"transcript_prefix": b"wrong prefix"})
    with pytest.raises(ValueError, match="prefix"):
        handler(tmp_path, mod=adapter).handle(bad)
    assert not adapter.calls


def test_adapter_cannot_change_frozen_inputs(tmp_path, arm_request):
    result = handler(tmp_path, mod=ScriptedAdapter(mutate=True)).handle(arm_request)
    mod = next(r for r in result.records if r.arm == "mod")
    assert mod.status == "INVALID"
    assert mod.context is None
    assert not mod.scorable


def test_recorded_summary_hash_must_match(tmp_path, arm_request):
    bad = arm_request.model_copy(update={"recorded_summary": "unverified summary"})
    with pytest.raises(ValueError, match="summary"):
        handler(tmp_path).handle(bad)


def test_refusal_observed_during_fork_switches_comparison(tmp_path, arm_request):
    class RefusingStock:
        def compact(self, request, workspace):
            return ModelCompactionObservation(
                reply="refused",
                transcript_retained=False,
                evidence="fork lost its transcript",
            )

    result = handler(tmp_path, stock=RefusingStock()).handle(
        arm_request.model_copy(update={"arms": ("stock_rerun", "mod"), "repeats": 2})
    )
    assert result.comparison_label == "RECORDED_STOCK"
    assert result.premise.evidence == "fork lost its transcript"
    assert not result.premise.accepted
    assert len([r for r in result.records if r.arm == "stock_recorded"]) == 2
    assert all(r.status == "REFUSED" for r in result.records if r.arm == "stock_rerun")


def test_ledger_hash_pin_and_duplicate_files_rejected(tmp_path, arm_request):
    from omnimarket.models.compaction_replay.model_compaction_manifest import (
        ModelLedgerFile,
    )

    bad = arm_request.model_copy(
        update={
            "ledger_files": (
                ModelLedgerFile(name="ledger.md", content=b"changed ledger\n"),
            )
        }
    )
    with pytest.raises(ValueError, match="ledger prefix"):
        handler(tmp_path).handle(bad)
    bad = arm_request.model_copy(update={"ledger_files": arm_request.ledger_files * 2})
    with pytest.raises(ValueError, match="ledger files"):
        handler(tmp_path).handle(bad)


def test_retry_appends_artifacts_and_keeps_first_bytes(tmp_path, arm_request):
    import json

    run = handler(tmp_path)
    first = run.handle(arm_request)
    saved = {r.artifact_path: Path(r.artifact_path).read_bytes() for r in first.records}
    second = run.handle(arm_request)
    assert {r.artifact_path for r in first.records}.isdisjoint(
        r.artifact_path for r in second.records
    )
    for path, content in saved.items():
        assert Path(path).read_bytes() == content
    stage = json.loads(Path(first.report_path).read_text())
    assert stage["records"][0]["prefix_hash"] == arm_request.case.prefix_sha256
    assert (
        first.records[0].compaction_input_hash == first.records[1].compaction_input_hash
    )


def test_adapter_exception_is_unavailable_and_not_scored(tmp_path, arm_request):
    class Broken:
        def compact(self, request, workspace):
            raise TimeoutError("adapter unavailable")

    result = handler(tmp_path, stock=Broken()).handle(arm_request)
    stock = next(r for r in result.records if r.arm == "stock_rerun")
    assert stock.status == "UNAVAILABLE"
    assert not stock.scorable
    assert result.comparison_label == "FRESH_STOCK"


def test_contract_registers_node_topics_ports_and_ci_gate():
    import importlib
    import tomllib

    import yaml

    root = Path(__file__).resolve().parents[3]
    node = root / "src/omnimarket/nodes/node_compaction_arm_run_effect"
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    registry = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    assert (
        registry[contract["name"]] == "omnimarket.nodes.node_compaction_arm_run_effect"
    )
    routed = contract["handler"]
    assert getattr(importlib.import_module(routed["module"]), routed["class"])
    assert contract["descriptor"]["node_archetype"] == "effect"
    assert contract["descriptor"]["purity"] == "effectful"
    assert contract["descriptor"]["idempotent"] is False
    assert set(contract["arm_adapters"]["controls"]) == {"oracle", "empty", "inverted"}
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]
    assert (
        contract["runtime_dispatch"]["command_topic"]
        in contract["event_bus"]["subscribe_topics"]
    )
    # Content must stay on the restricted command family, including completion.
    assert all(
        t.startswith("onex.cmd.omniintelligence.")
        for t in contract["event_bus"]["publish_topics"]
    )
    reply = contract["event_bus"]["request_response"]["instances"][0]
    assert reply["request_topic"] == contract["arm_runtime"]["pack_requested_topic"]
    assert (
        reply["reply_topics"]["completed"]
        == contract["arm_runtime"]["pack_built_topic"]
    )
    assert "Compaction arm producer" in (root / ".github/workflows/ci.yml").read_text()
    assert "compaction-arm-producer" in (root / ".pre-commit-config.yaml").read_text()


def test_shared_models_have_one_definition():
    from omnimarket.models.compaction_replay.model_ground_truth import (
        ModelGroundTruthResult,
    )
    from omnimarket.nodes.node_compaction_ground_truth_compute.models.model_ground_truth import (
        ModelGroundTruthResult as OldGroundTruth,
    )
    from omnimarket.nodes.node_context_compaction_manifest_compute.models.model_transcript_manifest import (
        ModelTranscriptCase as OldCase,
    )

    assert OldCase is ModelTranscriptCase
    assert OldGroundTruth is ModelGroundTruthResult


class ScriptedTerminalConsumer:
    """Subscribe-before-publish, immediate correlated terminals, no network."""

    def __init__(self):
        self.ids = []
        self.closed = []
        self.reply = None
        self.opened = []

    def open(self, topic):
        self.opened.append(topic)
        owner = self

        class Terminal:
            def register(self, cid):
                owner.ids.append(cid)

            def wait(self, cid, timeout):
                return owner.reply

            def close(self):
                owner.closed.append(topic)

        return Terminal()


def pinned_adapter(consumer, publish):
    import yaml
    from omnibase_core.models.context.model_context_access import ModelContextAccess

    from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
        HandlerPinnedPackAdapter,
    )

    node = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_compaction_arm_run_effect"
    )
    binding = yaml.safe_load((node / "contract.yaml").read_text())["arm_runtime"]
    return HandlerPinnedPackAdapter(
        publish,
        consumer,
        access=ModelContextAccess(tenant_id="fixture"),
        profile_name=binding["profile_name"],
        requested_topic=binding["pack_requested_topic"],
        built_topic=binding["pack_built_topic"],
        failed_topic=binding["pack_failed_topic"],
        timeout_s=0.1,
    )


def test_mod_calls_pinned_pack_and_retains_package_identity(tmp_path, arm_request):
    import json

    consumer = ScriptedTerminalConsumer()
    commands = []

    def publish(topic, body):
        envelope = json.loads(body)
        assert consumer.ids == [envelope["correlation_id"]] * 2
        commands.append(envelope["payload"])
        consumer.reply = {
            "correlation_id": envelope["correlation_id"],
            "payload": {
                "status": "ok",
                "profile_name": "compaction_orchestrator",
                "final_pack_hash": "a" * 64,
                "candidate_id": "00000000-0000-4000-8000-000000000001",
                "assembly_hash": "b" * 64,
                "body_hash": "c" * 64,
                "version_vector_hash": "d" * 64,
                "profile_content_hash": "e" * 64,
                "messages": [{"role": "user", "text": "pinned package"}],
            },
        }

    adapter = pinned_adapter(consumer, publish)
    result = handler(tmp_path, mod=adapter).handle(arm_request)
    (command,) = commands
    assert command["request"]["mode"] == "pinned"
    assert command["request"]["manifest_case"] == arm_request.case.case_key
    assert command["request"]["session_id"] == arm_request.case.session_id
    assert command["request"]["caller_scope"]["lane"] == "example"
    revisions = command["request"]["tier_revisions"]
    assert revisions["operator_rulings"] == arm_request.case.boundary_at
    assert revisions["operator_words_unrecorded"] == arm_request.case.prefix_sha256
    mod = next(r for r in result.records if r.arm == "mod")
    assert mod.context == "pinned package"
    assert mod.package_hash == "a" * 64
    assert mod.usage.measurement == "unavailable"
    assert mod.usage.wall_ms >= 0
    assert result.mod_fallback_rate == 0
    assert consumer.closed == list(reversed(consumer.opened))


def test_pack_native_reply_remains_in_mod_denominator(tmp_path, arm_request):
    from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
        HandlerModAdapter,
    )

    consumer = ScriptedTerminalConsumer()
    adapter = HandlerModAdapter(
        pinned_adapter(consumer, lambda _topic, _body: None),
        ScriptedAdapter(native=True),
    )
    result = handler(tmp_path, mod=adapter).handle(arm_request)
    mod = next(r for r in result.records if r.arm == "mod")
    assert result.mod_fallback_rate == 1
    assert mod.context == "native summary"
    assert mod.native_fallback
    assert consumer.closed == list(reversed(consumer.opened))


def test_wrong_pack_correlation_is_not_accepted(tmp_path, arm_request):
    consumer = ScriptedTerminalConsumer()
    consumer.reply = {"correlation_id": "another-command", "payload": {}}
    result = handler(
        tmp_path, mod=pinned_adapter(consumer, lambda _topic, _body: None)
    ).handle(arm_request)
    mod = next(r for r in result.records if r.arm == "mod")
    assert mod.status == "UNAVAILABLE"
    assert not mod.scorable


def test_native_adapter_forks_only_frozen_prefix(tmp_path, arm_request, monkeypatch):
    import json
    import re
    import subprocess
    from uuid import uuid4

    from omnimarket.models.compaction_replay.model_native_compaction_fork import (
        ModelNativeCompactionBinding,
        ModelNativeCompactionForkRequest,
    )
    from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_native_compaction_fork import (
        HandlerNativeCompactionFork,
    )

    sid, fid, uid = str(uuid4()), str(uuid4()), str(uuid4())
    prefix = (
        json.dumps(
            {
                "type": "user",
                "uuid": uid,
                "message": {"role": "user", "content": "frozen token"},
            }
        ).encode()
        + b"\n"
    )
    case = arm_request.case.model_copy(
        update={
            "session_id": sid,
            "prefix_sha256": sha(prefix),
            "prefix_bytes": len(prefix),
        }
    )
    arm_request = arm_request.model_copy(
        update={"case": case, "transcript_prefix": prefix}
    )
    store = tmp_path / "session-store"

    def run(argv, **kwargs):
        assert argv[1:3] == ["-p", "/compact"]
        assert "--fork-session" in argv
        assert argv[argv.index("--resume") + 1] == sid
        cwd = kwargs["cwd"]
        project = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd.resolve()))
        assert (store / project / (sid + ".jsonl")).read_bytes() == prefix
        boundary = {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": "boundary",
            "compactMetadata": {
                "trigger": "manual",
                "preTokens": 100,
                "postTokens": 20,
            },
        }
        summary = {
            "type": "user",
            "isCompactSummary": True,
            "message": {"content": "native context"},
        }
        (store / project / (fid + ".jsonl")).write_bytes(
            prefix
            + json.dumps(boundary).encode()
            + b"\n"
            + json.dumps(summary).encode()
            + b"\n"
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps({"session_id": fid}), "")

    monkeypatch.setattr(subprocess, "run", run)
    effect = HandlerNativeCompactionFork(
        ModelNativeCompactionBinding(
            workspace_root=tmp_path / "native-runs",
            executable="fixture-client",
            session_store=store,
            timeout_s=1,
        )
    )

    class Native:
        def compact(self, request, workspace):
            return effect.handle(
                ModelNativeCompactionForkRequest(
                    case=request.case, transcript_prefix=request.transcript_prefix
                )
            )

    native = Native()
    result = handler(tmp_path, stock=native).handle(arm_request)
    stock = next(r for r in result.records if r.arm == "stock_rerun")
    assert stock.context == "native context"
    assert stock.status == "OK"
    assert stock.usage.measurement == "estimated"
    assert stock.usage.input_tokens == 100
    assert stock.usage.post_tokens == 20
    assert Path(stock.adapter_artifacts[0]).is_file()


@pytest.fixture
async def arm_profile_bus():
    """One fixture and one assertion set on both declared transport profiles.

    Redpanda is an explicit lab invocation. Missing binding is an error, never
    a collected skip or a silent downgrade to the in-memory bus.
    """
    import os

    import yaml
    from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
    from omnibase_infra.event_bus.models.config import ModelKafkaEventBusConfig

    node = (
        Path(__file__).resolve().parents[3]
        / "src/omnimarket/nodes/node_compaction_arm_run_effect"
    )
    contract = yaml.safe_load((node / "contract.yaml").read_text())
    profiles = contract["test_profiles"]
    selected = os.environ.get(profiles["selector_env"], profiles["default"])
    if selected == "inmemory":
        bus = EventBusInmemory(environment="test", group="compaction-arm")
    elif selected == "redpanda":
        binding = profiles["redpanda"]
        broker = os.environ.get(binding["bootstrap_servers_env"])
        security = os.environ.get(binding["security_protocol_env"])
        if not broker or not security:
            raise RuntimeError(
                "redpanda profile requires declared broker and security-protocol overlay"
            )
        bus = EventBusKafka(
            config=ModelKafkaEventBusConfig(
                bootstrap_servers=broker,
                environment="test",
                timeout_seconds=10,
                security_protocol=security,
                sasl_mechanism=os.environ.get(binding["sasl_mechanism_env"]),
                sasl_plain_username=os.environ.get(binding["sasl_username_env"]),
                sasl_plain_password=os.environ.get(binding["sasl_password_env"]),
                auto_offset_reset="earliest",
                enable_idempotence=False,
            )
        )
    else:
        raise ValueError("unknown arm test profile")
    await bus.start()
    try:
        yield bus, contract
    finally:
        await bus.close()


@pytest.mark.asyncio
async def test_node_same_fixture_on_both_profiles(
    arm_profile_bus, tmp_path, arm_request
):
    import asyncio
    import json
    from uuid import uuid4

    from omnibase_core.models.container.model_onex_container import ModelONEXContainer

    from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run_runtime import (
        HandlerCompactionArmRunRuntime,
    )
    from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
        ModelArmRunResult,
    )
    from omnimarket.nodes.node_compaction_arm_run_effect.models.model_runtime_binding import (
        ModelArmRuntimeBinding,
    )
    from omnimarket.nodes.node_compaction_arm_run_effect.node import (
        NodeCompactionArmRunEffect,
    )

    bus, contract = arm_profile_bus
    settings = contract["arm_runtime"]
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
    node = NodeCompactionArmRunEffect(ModelONEXContainer())
    assert node is not None
    runtime = HandlerCompactionArmRunRuntime(
        binding=binding, stock=ScriptedAdapter(native=True), mod=ScriptedAdapter()
    )
    loop = asyncio.get_running_loop()
    terminal = loop.create_future()
    topics = contract["test_profiles"]["topics"]
    cid = str(arm_request.correlation_id)

    async def on_command(message):
        envelope = json.loads(message.value)
        if envelope.get("correlation_id") != cid:
            return
        result = await asyncio.to_thread(
            runtime.handle, ModelArmRunRequest.model_validate(envelope["payload"])
        )
        await bus.publish(
            topic=topics["completed"],
            key=None,
            value=json.dumps(
                {"correlation_id": cid, "payload": result.model_dump(mode="json")}
            ).encode(),
        )

    async def on_terminal(message):
        envelope = json.loads(message.value)
        if envelope.get("correlation_id") == cid and not terminal.done():
            terminal.set_result(ModelArmRunResult.model_validate(envelope["payload"]))

    command_unsub = await bus.subscribe(
        topic=topics["requested"],
        group_id="cm10-command-" + uuid4().hex,
        on_message=on_command,
    )
    terminal_unsub = await bus.subscribe(
        topic=topics["completed"],
        group_id="cm10-terminal-" + uuid4().hex,
        on_message=on_terminal,
    )
    try:
        await bus.publish(
            topic=topics["requested"],
            key=None,
            value=json.dumps(
                {"correlation_id": cid, "payload": arm_request.model_dump(mode="json")}
            ).encode(),
        )
        result = await asyncio.wait_for(terminal, timeout=30)
        assert [r.arm for r in result.records] == [
            "stock_rerun",
            "mod",
            "stock_recorded",
        ]
        assert all(r.scorable for r in result.records)
        assert len({r.initial_state_hash for r in result.records}) == 1
        assert result.correlation_id == arm_request.correlation_id
        assert result.mod_fallback_rate == 0
    finally:
        await terminal_unsub()
        await command_unsub()


def test_native_fork_adapter_uses_delegation_bus(tmp_path, arm_request):
    import json

    import yaml

    from omnimarket.nodes.node_compaction_arm_run_effect.handlers.arm_adapters import (
        HandlerNativeForkAdapter,
    )

    root = Path(__file__).resolve().parents[3]
    contract = yaml.safe_load(
        (
            root / "src/omnimarket/nodes/node_compaction_arm_run_effect/contract.yaml"
        ).read_text()
    )
    settings = contract["arm_runtime"]
    consumer = ScriptedTerminalConsumer()

    def publish(topic, data):
        assert topic == settings["native_requested_topic"]
        envelope = json.loads(data)
        assert consumer.ids == [envelope["correlation_id"]]
        assert (
            envelope["payload"]["case"]["prefix_sha256"]
            == arm_request.case.prefix_sha256
        )
        consumer.reply = {
            "correlation_id": envelope["correlation_id"],
            "payload": {
                "reply": "native",
                "context": "native through delegation",
                "evidence": "fixture fork",
            },
        }

    stock = HandlerNativeForkAdapter(
        publish,
        consumer,
        requested_topic=settings["native_requested_topic"],
        completed_topic=settings["native_completed_topic"],
        timeout_s=1,
    )
    result = handler(tmp_path, stock=stock).handle(arm_request)
    assert result.records[0].context == "native through delegation"
    delegation = yaml.safe_load(
        (
            root / "src/omnimarket/nodes/node_llm_delegation_call_effect/contract.yaml"
        ).read_text()
    )
    assert (
        settings["native_requested_topic"]
        in delegation["event_bus"]["subscribe_topics"]
    )
    assert (
        settings["native_completed_topic"] in delegation["event_bus"]["publish_topics"]
    )
    routed = next(
        r
        for r in delegation["handler_routing"]["handlers"]
        if r["operation"] == "native_compaction_fork"
    )
    assert routed["handler"]["name"] == "HandlerNativeCompactionFork"


def truth_for(arm_request):
    from omnimarket.models.compaction_replay.model_ground_truth import (
        ModelGroundTruthResult,
    )

    return ModelGroundTruthResult(
        session_id=arm_request.case.session_id,
        now=arm_request.case.boundary_at,
        window_start="2026-10-27T00:00:00Z",
        lane_family=("example",),
        prefix_row_count=1,
        rows_as_of_count=1,
        refused_row_count=0,
        prefix_hash=sha(b"frozen ledger"),
        set_hash=sha(b"ground truth"),
        counts_by_kind=(),
        items=(
            {
                "kind": "RULING",
                "key": "effective-ruling",
                "quote": "target rule with amender",
                "required_spans": ("target rule", "amending detail"),
                "expected_conduct": "not_act_against",
                "rows": (
                    {
                        "stamp": "2026-10-27T01:00:00Z",
                        "row_type": "RULING",
                        "row_id": "a" * 64,
                        "role": "primary",
                    },
                ),
            },
        ),
    )


def test_controls_keep_whole_effective_item_and_have_no_model_calls(
    tmp_path, arm_request
):
    truth = truth_for(arm_request)
    arm_request = arm_request.model_copy(
        update={"arms": ("oracle", "empty", "inverted"), "ground_truth": truth}
    )
    stock, mod = ScriptedAdapter(), ScriptedAdapter()
    result = handler(tmp_path, stock=stock, mod=mod).handle(arm_request)
    oracle, empty, inverted = result.records
    assert oracle.context == "target rule\namending detail"
    assert empty.context == ""
    assert inverted.context == "NOT (target rule\namending detail)"
    assert not stock.calls
    assert not mod.calls
    prior = handler(tmp_path).handle(
        arm_request.model_copy(
            update={"predecessors": {"effective-ruling": "predecessor rule"}}
        )
    )
    assert prior.records[-1].context == "predecessor rule"


def test_ground_truth_is_bound_to_pinned_ledger_and_boundary(tmp_path, arm_request):
    truth = truth_for(arm_request)
    for bad in (
        truth.model_copy(update={"prefix_hash": "f" * 64}),
        truth.model_copy(update={"now": "2026-10-28T00:00:00Z"}),
    ):
        with pytest.raises(ValueError, match="ground truth"):
            handler(tmp_path).handle(
                arm_request.model_copy(update={"ground_truth": bad})
            )


def test_ambiguous_pack_scope_is_refused_before_call(tmp_path, arm_request):
    consumer = ScriptedTerminalConsumer()
    case = arm_request.case.model_copy(
        update={
            "lane_attribution": arm_request.case.lane_attribution.model_copy(
                update={"lanes": ("first", "second")}
            )
        }
    )
    adapter = pinned_adapter(
        consumer,
        lambda _topic, _body: pytest.fail("ambiguous scope reached pack build"),
    )
    result = handler(tmp_path, mod=adapter).handle(
        arm_request.model_copy(update={"case": case})
    )
    mod = next(r for r in result.records if r.arm == "mod")
    assert mod.status == "REFUSED"
    assert not mod.scorable
    assert not consumer.opened
