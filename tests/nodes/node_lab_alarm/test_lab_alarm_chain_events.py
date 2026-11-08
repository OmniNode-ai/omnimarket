# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the lab alarm's broker lag condition (OMN-23024).

Golden: a watched group that does not grow between two samples ends in the completed event and keeps
its sample. Error: the named break, a watched group whose lag grows, ends in the typed failure event with
the group named; a node the running image does not carry, an unreadable manifest and an unreadable group
are INDETERMINATE and never a smaller list or a zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryLag,
    InMemoryManifest,
    InMemorySamples,
)
from omnimarket.nodes.node_lab_alarm.adapters.runtime_adapters import (
    FileSamples,
    HttpManifest,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    ConsumerGroupDerivationError,
    HandlerBrokerLag,
    derive_groups,
    events_for,
    load_topics,
    normalize_kafka_identifier,
    parse_command,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm import (
    EnumBrokerLagOutcome,
    EnumGroupState,
    EnumLabAlarmFault,
    ModelBrokerLagCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm.protocols import ManifestUnreadableError
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import run

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_TOPICS = ("onex.evt.example.one.v1", "onex.evt.example.two.v1")


def _group(version: str, topic: str) -> str:
    return (
        f"local.examplepkg.projection_example.consume.{version}"
        f".__i.runtime-main.__t.{topic}"
    )


def _overlay() -> ModelLabAlarmOverlay:
    return ModelLabAlarmOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


def _manifest(version: object = "1.2.0") -> dict[str, object]:
    return {
        "contracts": [
            {
                "name": "projection_example",
                "package_name": "examplepkg",
                "contract_version": version,
                "event_bus": {"subscribe_topics": list(_TOPICS)},
            },
            {  # a node nobody watches
                "name": "other_node",
                "package_name": "examplepkg",
                "contract_version": "1.0.0",
                "event_bus": {"subscribe_topics": ["onex.evt.example.three.v1"]},
            },
        ]
    }


def _handler(
    lags: dict[str, int],
    previous: dict[str, int] | None,
    manifest: InMemoryManifest | None = None,
) -> tuple[HandlerBrokerLag, InMemoryLag, InMemorySamples]:
    lag = InMemoryLag(lags)
    samples = InMemorySamples(previous)
    handler = HandlerBrokerLag(
        overlay=_overlay(),
        manifest=manifest or InMemoryManifest(_manifest()),
        lag=lag,
        samples=samples,
    )
    return handler, lag, samples


def _flat(value: int = 0) -> dict[str, int]:
    return {_group("1.2.0", t): value for t in _TOPICS}


def _chain(
    handler: HandlerBrokerLag, fault: EnumLabAlarmFault = EnumLabAlarmFault.NONE
):
    body = json.dumps({"correlation_id": "chain-1", "fault": fault.value}).encode()
    command = parse_command(body)
    assert isinstance(command, ModelBrokerLagCommand)
    result = handler.handle(command)
    return result, events_for(result, load_topics())


def test_golden_chain_no_group_grew_completes_and_keeps_the_sample() -> None:
    topics = load_topics()
    handler, _lag, samples = _handler(_flat(7), _flat(7))
    result, events = _chain(handler)

    assert [t for t, _ in events] == [
        topics["broker_lag_run_started"],
        topics["broker_lag_group_read"],
        topics["broker_lag_group_read"],
        topics["broker_lag_run_completed"],
    ]
    assert {body["correlation_id"] for _, body in events} == {"chain-1"}
    assert events[-1][0] == "onex.evt.omnimarket.lab-alarm-broker-lag-completed.v1"
    assert result.outcome is EnumBrokerLagOutcome.OK
    assert result.declared_groups == tuple(sorted(_flat()))
    assert samples.sample == _flat(7)


def test_a_high_but_flat_lag_is_not_an_alarm() -> None:
    handler, *_ = _handler(_flat(90_000), _flat(90_000))
    result, _ = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.OK


def test_a_falling_lag_is_not_an_alarm() -> None:
    handler, *_ = _handler(_flat(10), _flat(500))
    result, _ = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.OK


def test_the_first_run_is_a_baseline_and_reads_indeterminate() -> None:
    handler, _lag, samples = _handler(_flat(3), None)
    result, events = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.reasons == ("no-previous-sample",)
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]
    assert samples.sample == _flat(3)  # the baseline the next tick compares with


def test_error_chain_a_group_that_grew_alarms_and_names_the_group() -> None:
    lags = _flat(5)
    first = _group("1.2.0", _TOPICS[0])
    lags[first] = 9
    handler, *_ = _handler(lags, _flat(5))
    result, events = _chain(handler)

    assert events[-1][0] == "onex.evt.omnimarket.lab-alarm-broker-lag-failed.v1"
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert result.alarms == (f"{first}: lag grew 5->9",)
    assert events[-1][1]["failure"] == f"alarm:{first}: lag grew 5->9"


def test_error_chain_the_planted_lag_break_alarms_and_moves_no_baseline() -> None:
    """The named break: a watched group's lag grows between two samples."""
    handler, _lag, samples = _handler(_flat(5), _flat(5))
    result, events = _chain(handler, EnumLabAlarmFault.PLANTED_LAG)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]
    assert result.fault is EnumLabAlarmFault.PLANTED_LAG
    assert samples.sample == _flat(5)
    assert result.sample_kept is False


def test_error_chain_a_declared_node_the_image_lacks_is_indeterminate_not_a_smaller_list() -> (
    None
):
    handler, _lag, samples = _handler(_flat(5), _flat(5))
    result, events = _chain(handler, EnumLabAlarmFault.UNKNOWN_NODE)
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.readings == ()
    assert "planted-package/planted_node_the_image_does_not_carry" in result.reasons[0]
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]
    assert samples.sample == _flat(5)


def test_error_chain_an_unreadable_manifest_is_indeterminate() -> None:
    manifest = InMemoryManifest(_manifest())
    manifest.unreadable = "manifest fetch failed (URLError)"
    handler, *_ = _handler(_flat(5), _flat(5), manifest)
    result, events = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert result.reasons == ("derivation:manifest fetch failed (URLError)",)
    assert events[-1][0] == load_topics()["broker_lag_run_failed"]


def test_error_chain_an_unreadable_group_is_not_a_group_at_zero() -> None:
    handler, lag, samples = _handler(_flat(5), _flat(5))
    lag.refused[_group("1.2.0", _TOPICS[1])] = (
        "refused DESCRIBE (GroupAuthorizationFailedError)"
    )
    result, _ = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.INDETERMINATE
    assert any("refused DESCRIBE" in r for r in result.reasons)
    assert samples.sample == _flat(5)


def test_a_never_committed_group_is_informational_and_a_vanished_one_alarms() -> None:
    lags = _flat(5)
    quiet = _group("1.2.0", _TOPICS[1])
    handler, lag, _ = _handler(lags, {_group("1.2.0", _TOPICS[0]): 5})
    lag.never_committed.add(quiet)
    result, _ = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.OK
    assert [r.state for r in result.readings].count(EnumGroupState.NEVER_COMMITTED) == 1

    handler, lag, _ = _handler(lags, _flat(5))
    lag.never_committed.add(quiet)
    result, _ = _chain(handler)
    assert result.outcome is EnumBrokerLagOutcome.ALARM
    assert "offsets vanished" in result.alarms[0]


def test_a_contract_version_bump_changes_the_names_read_in_the_same_step() -> None:
    """The stale-list control: a literal list of 1.2.0 names reads nothing after a 1.3.0 bump."""
    manifest = InMemoryManifest(_manifest({"major": 1, "minor": 3, "patch": 0}))
    bumped = {_group("1.3.0", t): 0 for t in _TOPICS}
    handler, _lag, _samples = _handler(bumped, bumped, manifest)
    result, _ = _chain(handler)
    assert result.declared_groups == tuple(sorted(bumped))
    assert not set(result.declared_groups) & set(_flat())
    assert result.outcome is EnumBrokerLagOutcome.OK


def test_the_derivation_reproduces_a_live_runtime_group_name() -> None:
    manifest = {
        "contracts": [
            {
                "name": "projection_delegation",
                "package_name": "omnimarket",
                "contract_version": {"major": 1, "minor": 0, "patch": 0},
                "event_bus": {
                    "subscribe_topics": [
                        "onex.evt.omnimarket.delegate-skill-completed.v1"
                    ]
                },
            }
        ]
    }
    assert derive_groups(
        manifest,
        env="local",
        instance="runtime-main",
        nodes=[("omnimarket", "projection_delegation")],
    ) == (
        "local.omnimarket.projection_delegation.consume.1.0.0.__i.runtime-main"
        ".__t.onex.evt.omnimarket.delegate-skill-completed.v1",
    )


def test_a_plugin_managed_or_non_subscribing_contract_mints_no_group() -> None:
    manifest = _manifest()
    contracts = cast("list[dict[str, dict[str, object]]]", manifest["contracts"])
    contracts[0]["event_bus"]["plugin_managed"] = True
    with pytest.raises(ConsumerGroupDerivationError, match="projection_example"):
        derive_groups(
            manifest,
            env="local",
            instance="runtime-main",
            nodes=[("examplepkg", "projection_example")],
        )


def test_a_manifest_without_a_contracts_list_is_refused() -> None:
    with pytest.raises(ConsumerGroupDerivationError, match="no contracts list"):
        derive_groups({}, env="local", instance="x", nodes=[("a", "b")])


def test_identifier_normalisation_matches_the_runtime_rule() -> None:
    assert normalize_kafka_identifier("Runtime Main!") == "runtime_main"
    with pytest.raises(ConsumerGroupDerivationError):
        normalize_kafka_identifier("...")


def test_error_chain_a_bad_payload_is_a_typed_failure() -> None:
    assert parse_command(b'{"write": "maybe"}') == "bad-payload:correlation_id,write"


def test_the_contract_topics_are_the_chain_topics() -> None:
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["terminal_event"] == load_topics()["broker_lag_run_completed"]
    for role in (
        "broker_lag_run_started",
        "broker_lag_group_read",
        "broker_lag_run_completed",
        "broker_lag_run_failed",
    ):
        assert load_topics()[role] in contract["externally_consumed_topics"]


def test_the_example_overlay_loads_and_a_missing_or_extra_key_is_refused() -> None:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert ModelLabAlarmOverlay.model_validate(raw).groups.env == "local"
    for key in raw:
        missing = {k: v for k, v in raw.items() if k != key}
        with pytest.raises(ValueError, match=key):
            ModelLabAlarmOverlay.model_validate(missing)
    with pytest.raises(ValueError, match="surprise"):
        ModelLabAlarmOverlay.model_validate({**raw, "surprise": 1})
    twice = {**raw, "groups": {**raw["groups"], "nodes": raw["groups"]["nodes"] * 2}}
    with pytest.raises(ValueError, match="listed twice"):
        ModelLabAlarmOverlay.model_validate(twice)


def test_the_cli_run_reads_the_exit_code_of_the_outcome(tmp_path: Path) -> None:
    lines: list[str] = []
    handler, *_ = _handler(_flat(5), _flat(5))
    assert (
        run(
            "broker-lag",
            EnumLabAlarmFault.NONE,
            overlay=_overlay(),
            handler=handler,
            out=lines.append,
        )
        == 0
    )
    assert any(line.startswith("BROKER-LAG outcome=OK declared=2") for line in lines)

    handler, *_ = _handler(_flat(5), _flat(5))
    assert (
        run(
            "probe",
            EnumLabAlarmFault.PLANTED_LAG,
            overlay=_overlay(),
            handler=handler,
            out=lines.append,
        )
        == 2
    )
    handler, *_ = _handler(_flat(5), None)
    assert (
        run(
            "probe",
            EnumLabAlarmFault.NONE,
            overlay=_overlay(),
            handler=handler,
            out=lines.append,
        )
        == 3
    )


def test_the_probe_verb_keeps_no_sample() -> None:
    handler, _lag, samples = _handler(_flat(8), _flat(5))
    handler.handle(ModelBrokerLagCommand(correlation_id="p", write=False))
    assert samples.sample == _flat(5)


def test_the_file_sample_store_round_trips_and_ignores_a_damaged_file(
    tmp_path: Path,
) -> None:
    store = FileSamples(tmp_path / "state" / "sample.json")
    assert store.load() is None
    store.save({"g": 3})
    assert store.load() == {"g": 3}
    (tmp_path / "state" / "sample.json").write_text("not json", encoding="utf-8")
    assert store.load() is None


def test_the_http_manifest_names_a_failed_fetch_by_type_only() -> None:
    with pytest.raises(ManifestUnreadableError, match=r"manifest fetch failed \(\w+\)"):
        HttpManifest("http://127.0.0.1:1/v1/introspection/manifest", 1.0).read()
