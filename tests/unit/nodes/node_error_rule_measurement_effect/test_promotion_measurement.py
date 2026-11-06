# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measurement boundary controls; promotion policy itself belongs to core."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from omnibase_core.container import ModelONEXContainer

from omnimarket.models.error_rule_measurement import (
    ModelCommittedMeasurement,
    ModelLabelSet,
    ModelMatchLabel,
    ModelMeasurementCommand,
    ModelMeasurementOverlay,
    ModelRestatementLabel,
)
from omnimarket.nodes.node_error_rule_measurement_effect.handlers import (
    HandlerErrorRuleMeasurement,
)
from omnimarket.nodes.node_error_rule_measurement_effect.handlers.handler_daily import (
    artifact_digest,
)

NOW = datetime(2026, 11, 4, 21, tzinfo=UTC)


class Lab:
    def __init__(self):
        self.blobs = {}
        self.measurements = {}
        self.demotions = {}
        self.login = "measurement-app"
        self.tamper = False
        self.omit_readback = False
        self.labels_engine = "codex"
        self.confirmations = None
        self.captures = []
        self.matches = []
        self.row = {
            "id": "rule-a",
            "mode": "hint",
            "author_engine": "other",
            "definition": {
                "id": "rule-a",
                "class_keys": ["known"],
                "key_version": "version",
            },
            "live_since": (NOW - timedelta(days=3)).isoformat(),
            "hint_since": (NOW - timedelta(days=3)).isoformat(),
        }
        self.records = [
            {
                "record_id": f"r{i}",
                "lane": f"lane-{i % 2}",
                "occurred_at": (NOW - timedelta(days=i % 2)).isoformat(),
                "event": {"class": "known"},
            }
            for i in range(8)
        ]

    def put(self, content):
        digest = artifact_digest(content)
        self.blobs[digest] = content
        return digest

    def get(self, digest):
        return self.blobs[digest]

    def labels(self, request):
        return ModelLabelSet(
            engine=self.labels_engine,
            run_id="label-run",
            row_digest=request.row_digest,
            corpus_digest=request.corpus_digest,
            normalizer_digest=request.normalizer_digest,
            matches=tuple(
                ModelMatchLabel(record_id=r.record_id, applies=True)
                for r in request.sample
            ),
            restatements=tuple(
                ModelRestatementLabel(
                    capture_id=c.capture_id,
                    match_id=self.matches[0]["match_id"],
                    applies=True,
                )
                for c in request.captures
            ),
        )

    def replay(self, row, records, normalizer):
        return tuple(
            r.record_id for r in records if r.event["class"] in row["class_keys"]
        )

    def write(self, proposal):
        self.measurements[proposal.idempotency_key] = proposal
        return proposal.idempotency_key

    def read(self, key):
        if self.omit_readback:
            return None
        block = self.measurements[key].measured
        if self.tamper:
            block = block.model_copy(update={"true_matches": 100})
        return ModelCommittedMeasurement(
            commit_id=key,
            login=self.login,
            row_id=self.measurements[key].row_id,
            measured=block,
        )

    def demote(self, proposal):
        self.demotions[proposal.idempotency_key] = proposal
        return "demotion-pr"

    def command(self):
        def freeze(value):
            return self.put(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            )

        effective = artifact_digest(
            json.dumps(
                self.row["definition"], sort_keys=True, separators=(",", ":")
            ).encode()
        )
        matches = [
            dict(match, row_digest=match.get("row_digest", effective))
            for match in self.matches
        ]
        confirmations = self.confirmations
        if confirmations is None:
            confirmations = [
                {
                    "capture_id": c["capture_id"],
                    "match_id": self.matches[0]["match_id"],
                    "actor": "orchestrator",
                    "source_ref": "confirmation-1",
                    "confirmed_at": NOW.isoformat(),
                }
                for c in self.captures
            ]
        return ModelMeasurementCommand(
            now=NOW,
            row_digest=freeze(self.row),
            corpus_digest=freeze(self.records),
            normalizer_digest=freeze({"stages": ["input"]}),
            live_digest=freeze(
                {
                    "matches": matches,
                    "captures": self.captures,
                    "confirmations": confirmations,
                }
            ),
        )

    def handler(self):
        overlay = ModelMeasurementOverlay(
            sample_limit=50,
            freshness_days=7,
            restatement_window_days=7,
            schedule_seconds=86400,
            wilson_z=1.959963984540054,
            labeller_engine="codex",
            write_identity="measurement-app",
            allowed_write_logins=("measurement-app",),
            shadow_mode="shadow",
            hint_mode="hint",
            dispatch_mode="dispatch",
            confirmation_actors=("orchestrator",),
        )
        return HandlerErrorRuleMeasurement(
            overlay=overlay,
            artifacts=self,
            replay=self.replay,
            labeller=self.labels,
            writer=self,
        )


def test_promotion_measured_block_written_and_reread():
    lab = Lab()
    result = lab.handler().handle(lab.command())
    assert result.read_back
    assert result.measured.hits == 8
    assert result.measured.true_matches == 8
    assert result.measured.true_lanes == ("lane-0", "lane-1")
    assert len(result.measured.true_days) == 2
    assert result.measured.false_match_rate == 0
    assert result.measured.false_match_wilson.upper > 0
    assert result.measured.prospective is None
    assert lab.get(result.measured.labels_digest)


@pytest.mark.parametrize("kind", ["fabricated", "login", "missing"])
def test_promotion_evidence_readback_refuses(kind):
    lab = Lab()
    lab.tamper = kind == "fabricated"
    lab.omit_readback = kind == "missing"
    if kind == "login":
        lab.login = "another-login"
    with pytest.raises(ValueError, match=r"read.back|identity"):
        lab.handler().handle(lab.command())


def test_promotion_evidence_altered_artifact_refused():
    lab = Lab()
    command = lab.command()
    lab.blobs[command.corpus_digest] = b"[]"
    with pytest.raises(ValueError, match="digest"):
        lab.handler().handle(command)


def test_promotion_evidence_requires_second_labeller_engine():
    lab = Lab()
    lab.row["author_engine"] = "codex"
    with pytest.raises(ValueError, match=r"second.engine"):
        lab.handler().handle(lab.command())


def test_promotion_evidence_sample_has_weight_and_deterministic_ids():
    lab = Lab()
    lab.records *= 10
    for i, record in enumerate(lab.records):
        lab.records[i] = dict(record, record_id=f"r{i}")
    first = lab.handler().handle(lab.command())
    second = lab.handler().handle(lab.command())
    assert first == second
    assert first.measured.hits == 80
    assert first.measured.labelled_matches == 50
    assert first.measured.sample_weight == 1.6
    assert len(lab.measurements) == 1


def test_promotion_evidence_restatement_counted_and_demoted_once():
    lab = Lab()
    lab.matches = [
        {
            "match_id": "match-1",
            "row_id": "rule-a",
            "mode": "hint",
            "occurred_at": (NOW - timedelta(days=2)).isoformat(),
        }
    ]
    lab.captures = [
        {
            "capture_id": "capture-1",
            "occurred_at": (NOW - timedelta(days=1)).isoformat(),
            "text": "repeat rule",
        }
    ]
    first = lab.handler().handle(lab.command())
    second = lab.handler().handle(lab.command())
    assert first.measured.prospective.restatements == 1
    assert first.measured.prospective.miss_rate == 1
    assert first == second
    assert len(lab.demotions) == 1
    assert next(iter(lab.demotions.values())).target_mode == "shadow"


def test_promotion_evidence_shadow_has_no_prospective_rate():
    lab = Lab()
    lab.row["mode"] = "shadow"
    assert lab.handler().handle(lab.command()).measured.prospective is None


def test_promotion_evidence_labeller_cannot_confirm_its_own_restatement():
    lab = Lab()
    lab.matches = [
        {
            "match_id": "match-1",
            "row_id": "rule-a",
            "mode": "hint",
            "occurred_at": (NOW - timedelta(days=2)).isoformat(),
        }
    ]
    lab.captures = [
        {
            "capture_id": "capture-1",
            "occurred_at": (NOW - timedelta(days=1)).isoformat(),
            "text": "repeat rule",
        }
    ]
    # The label candidate is present, but no durable orchestrator confirmation.
    lab.confirmations = []
    result = lab.handler().handle(lab.command())
    assert result.measured.prospective.restatements == 0
    assert result.measured.prospective.pending_confirmation_ids == ("capture-1",)
    assert not lab.demotions


def test_promotion_evidence_false_matches_have_per_row_denominator_and_wilson():
    lab = Lab()
    original = lab.labels

    def labels(request):
        result = original(request)
        return result.model_copy(
            update={
                "matches": tuple(
                    label.model_copy(update={"applies": i >= 2})
                    for i, label in enumerate(result.matches)
                )
            }
        )

    lab.labels = labels
    measured = lab.handler().handle(lab.command()).measured
    assert measured.labelled_matches == 8
    assert measured.false_matches == 2
    assert measured.true_matches == 6
    assert measured.false_match_rate == 0.25
    assert measured.false_match_wilson.lower == pytest.approx(0.071479, abs=0.000001)
    assert measured.false_match_wilson.upper == pytest.approx(0.590725, abs=0.000001)
    assert len(measured.disagreements) == 2


def test_promotion_evidence_zero_matches_is_unavailable_not_a_zero_false_rate():
    lab = Lab()
    lab.records[0]["event"]["class"] = "different"
    lab.records = lab.records[:1]
    result = lab.handler().handle(lab.command())
    assert result.measured.hits == 0
    assert result.measured.false_match_rate is None
    assert result.measured.false_match_wilson is None
    assert result.measured.true_matches == 0
    assert result.measured.true_lanes == ()


@pytest.mark.parametrize(("count", "lanes", "age"), [(4, 2, 0), (8, 1, 0), (8, 2, 8)])
def test_promotion_evidence_sparse_single_lane_stale_are_reported_without_inventing_evidence(
    count, lanes, age
):
    lab = Lab()
    lab.records = lab.records[:count]
    for i, record in enumerate(lab.records):
        record["lane"] = f"lane-{i % lanes}"
        record["occurred_at"] = (NOW - timedelta(days=age + i % 2)).isoformat()
    measured = lab.handler().handle(lab.command()).measured
    assert measured.true_matches == count
    assert len(measured.true_lanes) == lanes
    assert measured.oldest_true_match <= NOW - timedelta(days=age)


def test_promotion_evidence_altered_label_artifact_fails_before_measurement_write():
    lab = Lab()
    original = lab.put

    def put(content):
        digest = original(content)
        if b'"run_id"' in content:
            lab.blobs[digest] = content.replace(b"label-run", b"other-run")
        return digest

    lab.put = put
    with pytest.raises(ValueError, match="digest"):
        lab.handler().handle(lab.command())
    assert lab.measurements == {}


def test_promotion_evidence_label_set_must_cover_exact_sample():
    lab = Lab()
    original = lab.labels
    lab.labels = lambda request: original(request).model_copy(update={"matches": ()})
    with pytest.raises(ValueError, match="exactly"):
        lab.handler().handle(lab.command())
    assert lab.measurements == {}


def live_lab(*, mode="hint", capture_day=2):
    lab = Lab()
    lab.row["mode"] = mode
    lab.row["live_since"] = (NOW - timedelta(days=10)).isoformat()
    lab.row["hint_since"] = (NOW - timedelta(days=10)).isoformat()
    lab.matches = [
        {
            "match_id": "match-1",
            "row_id": "rule-a",
            "mode": "hint",
            "occurred_at": (NOW - timedelta(days=9)).isoformat(),
        }
    ]
    lab.captures = [
        {
            "capture_id": "capture-1",
            "occurred_at": (NOW - timedelta(days=10 - capture_day)).isoformat(),
            "text": "repeat rule",
        }
    ]
    return lab


@pytest.mark.parametrize("day", [7, 8])
def test_promotion_evidence_restatement_seven_day_boundary(day):
    lab = live_lab(capture_day=day)
    measured = lab.handler().handle(lab.command()).measured
    assert measured.prospective.restatements == (1 if day == 7 else 0)
    assert len(lab.demotions) == (1 if day == 7 else 0)


def test_promotion_evidence_dispatch_retains_prospective_hint_observations():
    lab = live_lab(mode="dispatch")
    result = lab.handler().handle(lab.command())
    assert result.measured.prospective.live_matches == 1
    assert result.measured.prospective.restatements == 1
    assert result.demotion_pr == "demotion-pr"


def test_promotion_evidence_another_rule_revision_does_not_supply_hint_rate():
    lab = live_lab()
    lab.matches[0]["row_digest"] = "0" * 64
    with pytest.raises(ValueError, match="another row"):
        lab.handler().handle(lab.command())
    assert lab.demotions == {}


def test_promotion_evidence_crash_after_write_retries_same_measurement_pr():
    lab = Lab()
    command = lab.command()
    lab.omit_readback = True
    with pytest.raises(ValueError, match="read-back"):
        lab.handler().handle(command)
    assert len(lab.measurements) == 1
    lab.omit_readback = False
    assert lab.handler().handle(command).read_back
    assert len(lab.measurements) == 1


@pytest.mark.asyncio
async def test_promotion_measurement_canonical_runtime_bus_profile(
    tmp_path, monkeypatch
):
    """Run the contract's handler through the canonical adapter on either overlay.

    Selecting Redpanda is a mandatory proof, with no skip or silent fallback.
    The artifact and SCM ports are isolated controls on both transports.
    """
    import asyncio
    import importlib
    import os
    from contextlib import nullcontext
    from pathlib import Path
    from uuid import uuid4

    import yaml
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

    from omnimarket.models.error_rule_measurement import (
        ModelDailyMeasurementResult,
        ModelDailyMeasurementSnapshot,
        ModelMeasurementTick,
    )
    from tests.runtime_local_compat import LocalRuntimeBusAdapter

    directory = Path(__file__).parent
    root = Path(__file__).parents[4]
    profiles = yaml.safe_load((directory / "profiles.yaml").read_text())
    profile = profiles["profiles"][
        os.environ.get("ONEX_ERROR_MEASUREMENT_PROFILE", profiles["default"])
    ]
    binding = nullcontext()
    if profile["event_bus"] == "kafka":
        from omnibase_infra.cli.delegate_lane import resolve_lane_selection_at
        from omnibase_infra.cli.delegate_lane_credentials import (
            resolve_lane_client_transport_for,
        )
        from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka
        from omnibase_infra.event_bus.lane_client_transport_binding import (
            bind_lane_client_transport,
        )

        from tests.bus_lane_guard import (
            assert_broker_in_declared_lane,
            declared_test_lane,
            resolve_test_bus_broker,
        )

        assert profile["broker_source"] == "declared_test_bus_lane"
        broker = resolve_test_bus_broker()
        assert_broker_in_declared_lane(broker)
        selection = resolve_lane_selection_at(declared_test_lane(), root)
        assert selection.bootstrap_servers == broker
        transport = resolve_lane_client_transport_for(
            lane_target=selection, onex_home=Path.home() / ".onex", environ=os.environ
        )
        binding = (
            bind_lane_client_transport(transport)
            if transport is not None
            else nullcontext()
        )
        with binding:
            bus = EventBusKafka.from_bootstrap(broker)
    else:
        assert profile["event_bus"] == "inmemory"
        bus = EventBusInmemory(
            environment="measurement-proof", group="measurement-proof"
        )
    contract = yaml.safe_load(
        (
            root
            / "src/omnimarket/nodes/node_error_rule_measurement_effect/contract.yaml"
        ).read_text()
    )
    spec = contract["handler"]
    handler_type = getattr(importlib.import_module(spec["module"]), spec["class"])
    lab = live_lab()
    injected = lab.handler()
    command = lab.command()

    class Source:
        def snapshot(self, now):
            return ModelDailyMeasurementSnapshot(
                row_digests=(command.row_digest,),
                corpus_digest=command.corpus_digest,
                normalizer_digest=command.normalizer_digest,
                live_digest=command.live_digest,
            )

    from omnimarket.protocols.protocol_error_rule_measurement import (
        ProtocolMeasurementArtifacts,
        ProtocolMeasurementLabeller,
        ProtocolMeasurementReplay,
        ProtocolMeasurementSource,
        ProtocolMeasurementWriter,
    )

    overlay_dir = tmp_path / "node_error_rule_measurement_effect"
    overlay_dir.mkdir()
    (overlay_dir / "overlay.yaml").write_text(
        yaml.safe_dump(injected.overlay.model_dump(mode="json"))
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    container = ModelONEXContainer()
    for protocol, service in (
        (ProtocolMeasurementSource, Source()),
        (ProtocolMeasurementArtifacts, lab),
        (ProtocolMeasurementReplay, lab.replay),
        (ProtocolMeasurementLabeller, lab.labels),
        (ProtocolMeasurementWriter, lab),
    ):
        await container.service_registry.register_instance(protocol, service)
    handler = handler_type(container=container)
    request = ModelMeasurementTick(now=NOW)
    expected = await handler.handle(request)
    suffix = uuid4().hex
    command_topic = (
        contract["runtime_dispatch"]["command_topic"].removesuffix(".v1")
        + f".{suffix}.v1"
    )
    assert contract["terminal_event"] == "onex.evt.omnimarket.error-rule-measured.v1"
    output_topic = contract["terminal_event"].removesuffix(".v1") + f".{suffix}.v1"
    results = asyncio.Queue()
    errors = []
    adapter = LocalRuntimeBusAdapter(
        handler=handler,
        handler_name=spec["class"],
        input_model_cls=ModelMeasurementTick,
        output_topic=output_topic,
        bus=bus,
        on_error=lambda: errors.append("adapter failed"),
    )

    async def observe(message: ModelEventMessage):
        await results.put(
            ModelDailyMeasurementResult.model_validate_json(message.value)
        )

    await bus.start()
    try:
        stop_observe = await bus.subscribe_ungrouped(output_topic, observe)
        stop_handler = await bus.subscribe_ungrouped(command_topic, adapter.on_message)
        try:
            await bus.publish(
                command_topic,
                command.row_digest.encode(),
                request.model_dump_json().encode(),
            )
            actual = await asyncio.wait_for(results.get(), timeout=30)
            assert not errors
            assert (
                actual.model_dump_json().encode() == expected.model_dump_json().encode()
            )
            assert len(lab.measurements) == 1
            assert len(lab.demotions) == 1
        finally:
            await stop_handler()
            await stop_observe()
    finally:
        await bus.close()
    target = os.environ.get("ONEX_ERROR_MEASUREMENT_RESULT_FILE")
    if target:
        Path(target).write_text(actual.model_dump_json())


def test_promotion_evidence_missing_capture_label_cannot_report_zero_misses():
    lab = live_lab()
    original = lab.labels
    lab.labels = lambda request: original(request).model_copy(
        update={"restatements": ()}
    )
    with pytest.raises(ValueError, match="every operator capture"):
        lab.handler().handle(lab.command())


def test_promotion_evidence_future_corpus_record_cannot_supply_true_matches():
    lab = Lab()
    lab.records[0]["occurred_at"] = (NOW + timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future corpus"):
        lab.handler().handle(lab.command())


def test_promotion_evidence_label_artifact_altered_during_commit_is_refused():
    lab = Lab()
    original = lab.write

    def write(proposal):
        commit_id = original(proposal)
        lab.blobs[proposal.measured.labels_digest] = b"{}"
        return commit_id

    lab.write = write
    with pytest.raises(ValueError, match="digest"):
        lab.handler().handle(lab.command())


async def test_promotion_measurement_daily_sweep_measures_every_current_row():
    from omnimarket.models.error_rule_measurement import (
        ModelDailyMeasurementSnapshot,
        ModelMeasurementTick,
    )
    from omnimarket.nodes.node_error_rule_measurement_effect.handlers.handler_daily import (
        HandlerErrorRuleMeasurementDaily,
    )

    lab = Lab()
    first = lab.command()
    lab.row["id"] = "rule-b"
    lab.row["definition"]["id"] = "rule-b"
    second = lab.command()

    class Source:
        def snapshot(self, now):
            assert now == NOW
            return ModelDailyMeasurementSnapshot(
                row_digests=(first.row_digest, second.row_digest),
                corpus_digest=first.corpus_digest,
                normalizer_digest=first.normalizer_digest,
                live_digest=first.live_digest,
            )

    injected = lab.handler()
    daily = HandlerErrorRuleMeasurementDaily(
        container=ModelONEXContainer(),
        overlay=injected.overlay,
        artifacts=lab,
        replay=lab.replay,
        labeller=lab.labels,
        writer=lab,
        source=Source(),
    )
    result = await daily.handle(ModelMeasurementTick(now=NOW))
    assert len(result.measurements) == 2
    assert len(lab.measurements) == 2
    assert result.measured_at == NOW


def test_promotion_evidence_shadow_does_not_label_prospective_capture_rows():
    lab = live_lab(mode="shadow")
    result = lab.handler().handle(lab.command())
    assert result.measured.prospective is None
    assert lab.demotions == {}


def test_promotion_evidence_unconfirmed_or_unauthorized_confirmation_cannot_demote():
    lab = live_lab()
    lab.confirmations = [
        {
            "capture_id": "capture-1",
            "match_id": "match-1",
            "actor": "labeller",
            "source_ref": "not-orchestrator",
            "confirmed_at": NOW.isoformat(),
        }
    ]
    with pytest.raises(ValueError, match="confirmation provenance"):
        lab.handler().handle(lab.command())
    assert lab.demotions == {}


async def test_promotion_daily_handler_wires_through_runtime_container():
    from pathlib import Path

    from omnibase_core.container import ModelONEXContainer
    from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
    from omnibase_infra.runtime.auto_wiring.discovery import (
        discover_contracts_from_paths,
    )
    from omnibase_infra.runtime.auto_wiring.handler_wiring import wire_from_manifest
    from omnibase_infra.runtime.auto_wiring.models import ModelAutoWiringManifest
    from omnibase_infra.runtime.auto_wiring.report import EnumWiringOutcome
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    contract = (
        Path(__file__).resolve().parents[4]
        / "src/omnimarket/nodes/node_error_rule_measurement_effect/contract.yaml"
    )
    discovered = discover_contracts_from_paths([contract])
    assert not discovered.errors
    assert len(discovered.contracts) == 1
    bus = EventBusInmemory()
    try:
        report = await wire_from_manifest(
            ModelAutoWiringManifest(contracts=tuple(discovered.contracts)),
            MessageDispatchEngine(),
            event_bus=bus,
            container=ModelONEXContainer(),
            environment="local",
        )
    finally:
        await bus.close()
    assert report.total_wired == 1
    assert not report.quarantined_handlers
    assert all(result.outcome != EnumWiringOutcome.FAILED for result in report.results)


async def test_promotion_daily_unbound_source_refuses_without_writing():
    from omnibase_core.errors import ModelOnexError

    from omnimarket.models.error_rule_measurement import ModelMeasurementTick
    from omnimarket.nodes.node_error_rule_measurement_effect.handlers.handler_daily import (
        HandlerErrorRuleMeasurementDaily,
    )

    lab = Lab()
    daily = HandlerErrorRuleMeasurementDaily(
        container=ModelONEXContainer(), overlay=lab.handler().overlay
    )
    with pytest.raises(ModelOnexError, match="ProtocolMeasurementSource"):
        await daily.handle(ModelMeasurementTick(now=NOW))
    assert lab.measurements == {}
    assert lab.demotions == {}
