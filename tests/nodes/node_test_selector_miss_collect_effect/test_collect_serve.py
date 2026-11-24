# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The collector's own serve process: one pass answers the collect_nightly command the nightly sender sends.

The nightly sender publishes ``collect_nightly`` in an event envelope on the contract's command topic and waits for the
terminal event that names it (``ScmBusCaller``). Nothing consumed that topic: the node subscribes to nothing (its handlers
take ports as constructor parameters). The serve tick reads the command topic, builds the handler with ports,
runs it and publishes the answer the sender's matcher accepts. The registry unit starts the tick on the host
the overlay's serve block names.
"""

from __future__ import annotations

import importlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.delegated_test_loop.lab_run_bus import envelope_bytes_for
from omnimarket.handlers.scm.scm_terminal_match import terminal_payload
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.file_alarm_store import (
    FileAlarmStore,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
    InMemoryNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers import (
    HandlerCollectNightly,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    EnumAlarmKind,
    ModelMissCollectSettings,
    ModelNightlyAlarm,
    load_policy,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.runtime_collect_serve import (
    EXIT_OK,
    main,
    run_once,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
NODE = NODES / "node_test_selector_miss_collect_effect"
REGISTRY = NODES / "node_host_installer_registry_units" / "contract.yaml"
UNIT = "test-selector-collect-serve-systemd"
SLOT = date(2026, 11, 23)
NOW = datetime(2026, 11, 24, 3, 20, tzinfo=UTC)
SERVE = {
    "bus_lane": "dev",
    "workspace_root": "$OMNIBASE_PATH",
    "consumer_group": "local.omnimarket.node_test_selector_miss_collect_effect.consume.v1.h053",
    "max_records": 10,
    "read_timeout_s": 20.0,
    "join_timeout_s": 15.0,
    "commit_clone": "$OMNI_HOME/omnimarket",
    "state_dir": "$OMNI_HOME/.onex_state/test-selector-collect-serve",
}
OVERLAY = {
    "repo": "example-org/example-repo",
    "workflow_file": "nightly-full-suite.yml",
    "owner_role": "example-test-selection-owner",
    "alarm_route": "test_selector_miss",
    "collector_enabled": True,
    "status_tick": "example-lab-status-tick",
}


def _contract() -> dict[str, Any]:
    return yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))


def _command_topic() -> str:
    return str(_contract()["runtime_dispatch"]["command_topic"])


def _terminal_topic() -> str:
    return str(_contract()["terminal_event"])


def _handler() -> HandlerCollectNightly:
    return HandlerCollectNightly(
        settings=ModelMissCollectSettings.load(OVERLAY),
        policy=load_policy(),
        gateway=InMemoryNightlyGateway(run=None, reading=None, previous=None),
        store=InMemoryAlarmStore(),
        receiver=InMemoryAlarmReceiver(),
    )


class _Reader:
    def __init__(self, records: list[str]) -> None:
        self.records = records
        self.topics: list[str] = []

    def read(self, topic: str) -> list[str]:
        self.topics.append(topic)
        return self.records


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, str | None]] = []

    def publish(self, topic: str, body: bytes, key: str | None = None) -> None:
        self.sent.append((topic, body, key))


def _command(correlation: uuid.UUID) -> str:
    return envelope_bytes_for(
        _command_topic(),
        {"slot_date": SLOT.isoformat(), "now": NOW.isoformat()},
        correlation,
    ).decode()


def test_serve_reads_the_collect_command_topic_the_contract_names() -> None:
    reader = _Reader([])
    run_once(_handler(), reader, _Publisher())
    assert reader.topics == [_command_topic()]


def test_serve_answers_with_a_terminal_the_senders_matcher_accepts() -> None:
    correlation = uuid.uuid4()
    publisher = _Publisher()
    evidence = run_once(_handler(), _Reader([_command(correlation)]), publisher)
    assert evidence.answered == 1
    (topic, body, key) = publisher.sent[0]
    assert topic == _terminal_topic()
    assert key == str(correlation)
    raw = json.loads(body)
    named = terminal_payload(raw, uuid.uuid4(), correlation)
    assert named is not None
    # An in-memory gateway with no run reads as no_run: the answer's shape is what the sender validates.
    assert named["status"] == "no_run"
    assert named["slot_date"] == SLOT.isoformat()


def test_serve_counts_an_unreadable_record_and_publishes_nothing_for_it() -> None:
    publisher = _Publisher()
    evidence = run_once(_handler(), _Reader(["not json", "{}"]), publisher)
    assert (evidence.records, evidence.answered, evidence.unreadable) == (2, 0, 2)
    assert publisher.sent == []


def test_serve_main_probe_exits_before_any_overlay_or_bus_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--probe"]) == EXIT_OK
    assert json.loads(capsys.readouterr().out) == {"status": "probe_ok"}


def test_serve_main_without_an_overlay_exits_unread_and_names_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_TEST_SELECTOR_MISS_COLLECT_OVERLAY", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert main([]) == 3
    assert json.loads(capsys.readouterr().out)["status"] == "overlay_refused"


def test_the_overlay_carries_an_optional_serve_block_and_still_forbids_other_keys() -> (
    None
):
    with_serve = ModelMissCollectSettings.load({**OVERLAY, "serve": SERVE})
    assert with_serve.serve is not None
    assert with_serve.serve.bus_lane == "dev"
    assert ModelMissCollectSettings.load(OVERLAY).serve is None
    with pytest.raises(ValueError, match="serve"):
        ModelMissCollectSettings.load({**OVERLAY, "serve": {**SERVE, "extra_key": 1}})


def test_the_example_overlay_declares_the_serve_block() -> None:
    example = yaml.safe_load(
        (NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert ModelMissCollectSettings.load(example).serve is not None


def _alarm(test_file: str = "*") -> ModelNightlyAlarm:
    return ModelNightlyAlarm(
        repo="example-org/example-repo",
        slot_date=SLOT,
        test_file=test_file,
        kind=EnumAlarmKind.INCOMPLETE,
        owner_role="example-test-selection-owner",
        route="test_selector_miss",
        text="the night is incomplete",
    )


def test_the_file_alarm_store_keeps_a_row_across_two_passes(tmp_path: Path) -> None:
    first = FileAlarmStore(tmp_path)
    assert first.upsert_pending(_alarm()) is True
    # A second pass is a new process: a new store over the same directory.
    second = FileAlarmStore(tmp_path)
    assert second.upsert_pending(_alarm()) is False
    assert [a.key for a in second.undelivered()] == [_alarm().key]
    second.mark_delivered(_alarm().key)
    assert FileAlarmStore(tmp_path).undelivered() == ()
    assert (
        FileAlarmStore(tmp_path).has_collector_result("example-org/example-repo", SLOT)
        is False
    )
    second.record_collector_result("example-org/example-repo", SLOT)
    assert (
        FileAlarmStore(tmp_path).has_collector_result("example-org/example-repo", SLOT)
        is True
    )


# --- the registry unit ---------------------------------------------------------------------------


def _unit() -> dict[str, Any]:
    units = yaml.safe_load(REGISTRY.read_text(encoding="utf-8"))["units"]
    assert UNIT in units, f"the registry declares no unit {UNIT}"
    return dict(units[UNIT])


def test_the_registry_declares_the_collector_serve_unit_on_a_systemd_host() -> None:
    unit = _unit()
    assert unit["platform"] == "systemd"
    assert unit["kind"] == "registry_module_tick"
    assert unit["probe_argv"] == ["--probe"]
    assert unit["environment_refuse_worktree_paths"] == ["ONEX_SKILL_OVERLAY_ROOTS"]


def test_the_unit_starts_the_module_whose_main_the_node_ships() -> None:
    unit = _unit()
    assert (NODES.parents[2] / unit["entry_file"]).is_file()
    module = importlib.import_module(unit["module"])
    assert callable(module.main)


def test_the_unit_run_limit_covers_the_bus_bounds_and_one_gateway_pass() -> None:
    serve = SERVE
    spent = serve["join_timeout_s"] + serve["read_timeout_s"]
    assert _unit()["schedule"]["run_timeout_s"] >= spent + 300


def test_the_unit_reads_the_overlay_roots_and_names_the_overlay_clone() -> None:
    environment = _unit()["environment"]
    assert environment["ONEX_SKILL_OVERLAY_ROOTS"] == "{overlays_root}"
