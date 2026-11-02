# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21818 -- the guard deletes long-Empty per-call groups under the contract's limits, and nothing else."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from omnimarket.models.consumer_group_guard import ModelReapRules
from omnimarket.nodes.node_consumer_group_guard_effect import (
    runtime_consumer_group_guard as runtime,
)
from omnimarket.nodes.node_consumer_group_guard_effect.handlers import (
    handler_group_reaper as reaper_module,
)
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_group_reaper import (
    HandlerConsumerGroupReaper,
    contract_reap_rules,
    is_reapable,
)

pytestmark = pytest.mark.unit

RULES = contract_reap_rules()
LEDGER = "local.omnimarket.work_ledger_append_client.consume.v1.0070302c2314.__t.onex.evt.omnimarket.work-ledger-append-failed.v1"
SCM = "local.omnimarket.scm_command_client.consume.v1.012531d317c1.__t.onex.evt.omnimarket.github-repo-write-completed.v1"
TERMINAL = "local.omnibase_core.runtime_local_terminal_run_0043eb9dd332_t1.consume.v1.__t.onex.evt.omnimarket.delegate-skill-failed.v1"
DEVHEAD = "local.omnibase_core.handlerdevheadmonitor_run_06ec2890bd50.consume.v1.__t.onex.cmd.omniclaude.dev-head-monitor.v1"
TERMINAL_T = "local.omnibase_core.runtime_local_terminal_t1_run_0066d2acb6bd.consume.v1.__t.onex.evt.omnimarket.delegate-skill-failed.v1"
LAB_WORK = "local.omnimarket.lab_work_client.consume.v1.0123456789ab.cap.__t.onex.evt.omnimarket.lab-work-capacity.v1"
RECEIPT = "receipt-mode-client.5525153f0632.__t.onex.evt.omnimarket.context-compaction-prepared.v1"
CANARY = "gateway-canary-probe-local-56b55982360f41b49a7eed46e752773f"
DRIFT_RUN = "local.example_fleet.host_drift_fleet.h105.0123456789ab.__t.onex.evt.omnimarket.host-drift-heartbeat.v1"
DRIFT = "local.example_fleet.host_drift_fleet.h105.__t.onex.evt.omnimarket.host-drift-heartbeat.v1"
GATEWAY = "onex.gateway-session.dc01928a-654e-4568-b42e-0123456789ab"
STABLE = "agent-observability-postgres"
PROJECTION = "local.omnimarket.projection_work_ledger.consume.1.1.1.__i.runtime-main.__t.onex.evt.omnimarket.x.v1"
PER_CALL = [LEDGER, SCM, TERMINAL, DEVHEAD]
# One live name per family the dev and CI buses accumulate (OMN-21288).
LIVE_PER_RUN = [
    TERMINAL,
    TERMINAL_T,
    DEVHEAD,
    LAB_WORK,
    RECEIPT,
    CANARY,
    DRIFT_RUN,
    GATEWAY,
]
STABLE_NAMES = ["onex-deploy-agent", "onex-dlq-replay", DRIFT, PROJECTION, STABLE]
CONFIG: Mapping[str, str] = {}


class Broker:
    """In-memory broker: groups by state, a delete log and a scripted health."""

    def __init__(self, states: dict[str, str]) -> None:
        self.states = dict(states)
        self.deleted: list[str] = []
        self.batches: list[int] = []
        self.health_calls = 0
        self.unhealthy_from = 10**9
        self.error_name = ""

    def read(self, _config: Mapping[str, str]) -> dict[str, str]:
        return dict(self.states)

    def delete(self, _config: Mapping[str, str], names: list[str]) -> dict[str, str]:
        self.batches.append(len(names))
        out: dict[str, str] = {}
        for name in names:
            if self.error_name:
                out[name] = self.error_name
            elif self.states.get(name) != "EMPTY":
                out[name] = "NON_EMPTY_GROUP"  # the broker refuses a group with members
            else:
                del self.states[name]
                self.deleted.append(name)
                out[name] = ""
        return out

    def health(self, _config: Mapping[str, str]) -> str:
        self.health_calls += 1
        return "" if self.health_calls < self.unhealthy_from else "broker unreadable: X"


def _reaper(
    broker: Broker, clock: list[float], rules: ModelReapRules = RULES
) -> HandlerConsumerGroupReaper:
    return HandlerConsumerGroupReaper(
        broker.read, broker.delete, broker.health, lambda: rules, lambda: clock[0]
    )


def _mixed() -> dict[str, str]:
    states = dict.fromkeys(PER_CALL, "EMPTY")
    states.update({DRIFT: "EMPTY", STABLE: "EMPTY"})
    states[f"{LEDGER}.live"] = "STABLE"
    return states


def test_the_limits_and_names_are_declared_in_the_contract() -> None:
    assert RULES.enabled
    assert RULES.min_empty_age_seconds == 600
    assert RULES.max_deletes_per_run > 0
    assert all(is_reapable(n, RULES) for n in PER_CALL)
    assert not any(is_reapable(n, RULES) for n in (DRIFT, STABLE))


def test_every_live_per_run_family_is_reapable_and_stable_names_are_not() -> None:
    assert all(is_reapable(n, RULES) for n in LIVE_PER_RUN)
    assert not any(is_reapable(n, RULES) for n in STABLE_NAMES)


def test_the_first_run_deletes_nothing_and_a_run_ten_minutes_later_deletes_the_per_call_groups(
    tmp_path: Path,
) -> None:
    broker, clock = Broker(_mixed()), [1000.0]
    reaper = _reaper(broker, clock)
    assert reaper.reap(CONFIG, tmp_path)[0] == 0
    clock[0] += 300
    assert reaper.reap(CONFIG, tmp_path)[0] == 0
    clock[0] += 300
    deleted, detail = reaper.reap(CONFIG, tmp_path)
    assert deleted == len(PER_CALL)
    assert sorted(broker.deleted) == sorted(PER_CALL)
    assert f"reaped {len(PER_CALL)}" in detail


def test_falsifier_a_stable_named_or_non_empty_group_is_never_deleted(
    tmp_path: Path,
) -> None:
    broker, clock = Broker(_mixed()), [0.0]
    reaper = _reaper(broker, clock)
    for _ in range(5):
        reaper.reap(CONFIG, tmp_path)
        clock[0] += 600
    survivors = set(broker.states)
    assert {DRIFT, STABLE, f"{LEDGER}.live"} <= survivors
    assert not set(broker.deleted) & {DRIFT, STABLE, f"{LEDGER}.live"}


def test_protected_wins_over_reapable() -> None:
    both = ModelReapRules(
        enabled=True,
        min_empty_age_seconds=0,
        max_deletes_per_run=10,
        batch_size=10,
        reapable_patterns=(r"^x\.[0-9a-f]{12}$",),
        protected_patterns=("^x\\.",),
    )
    assert not is_reapable("x.0123456789ab", both)


def test_a_group_that_left_empty_and_returned_starts_its_age_again(
    tmp_path: Path,
) -> None:
    broker, clock = Broker({LEDGER: "EMPTY"}), [0.0]
    reaper = _reaper(broker, clock)
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 500
    broker.states[LEDGER] = "STABLE"
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 700
    broker.states[LEDGER] = "EMPTY"
    assert reaper.reap(CONFIG, tmp_path)[0] == 0
    assert broker.deleted == []


def test_a_group_that_was_never_seen_empty_is_not_deleted_the_run_it_first_appears(
    tmp_path: Path,
) -> None:
    broker, clock = Broker({}), [0.0]
    reaper = _reaper(broker, clock)
    reaper.reap(CONFIG, tmp_path)
    broker.states[LEDGER] = "EMPTY"
    clock[0] = 10_000
    assert reaper.reap(CONFIG, tmp_path)[0] == 0


def test_a_run_is_bounded_and_deletes_in_batches_with_health_between(
    tmp_path: Path,
) -> None:
    rules = RULES.model_copy(update={"max_deletes_per_run": 5, "batch_size": 2})
    names = [
        f"local.omnimarket.scm_command_client.consume.v1.{i:012x}" for i in range(9)
    ]
    broker, clock = Broker(dict.fromkeys(names, "EMPTY")), [0.0]
    reaper = _reaper(broker, clock, rules)
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 600
    broker.health_calls = 0
    assert reaper.reap(CONFIG, tmp_path)[0] == 5
    assert broker.batches == [2, 2, 1]
    assert broker.health_calls == 1 + 3  # before the first batch and after each


def test_an_unhealthy_broker_after_a_batch_stops_the_run(tmp_path: Path) -> None:
    rules = RULES.model_copy(update={"batch_size": 1})
    broker, clock = Broker(dict.fromkeys(PER_CALL, "EMPTY")), [0.0]
    reaper = _reaper(broker, clock, rules)
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 600
    broker.health_calls = 0
    broker.unhealthy_from = (
        3  # healthy before, healthy after batch one, sick after batch two
    )
    deleted, detail = reaper.reap(CONFIG, tmp_path)
    assert deleted == 2
    assert "not healthy after a batch" in detail


def test_an_unhealthy_broker_before_the_first_batch_deletes_nothing(
    tmp_path: Path,
) -> None:
    broker, clock = Broker({LEDGER: "EMPTY"}), [0.0]
    reaper = _reaper(broker, clock)
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 600
    broker.unhealthy_from = 1
    assert reaper.reap(CONFIG, tmp_path)[0] == 0
    assert broker.deleted == []


def test_the_first_delete_error_stops_the_run(tmp_path: Path) -> None:
    rules = RULES.model_copy(update={"batch_size": 1})
    broker, clock = Broker(dict.fromkeys(PER_CALL, "EMPTY")), [0.0]
    reaper = _reaper(broker, clock, rules)
    reaper.reap(CONFIG, tmp_path)
    clock[0] = 600
    broker.error_name = "COORDINATOR_NOT_AVAILABLE"
    deleted, detail = reaper.reap(CONFIG, tmp_path)
    assert deleted == 0
    assert broker.batches == [1]
    assert "COORDINATOR_NOT_AVAILABLE" in detail


def test_an_unreadable_listing_deletes_nothing(tmp_path: Path) -> None:
    def read(_c: Mapping[str, str]) -> dict[str, str]:
        raise RuntimeError("password=hunter2")

    broker = Broker({LEDGER: "EMPTY"})
    reaper = HandlerConsumerGroupReaper(
        read, broker.delete, broker.health, lambda: RULES, lambda: 0.0
    )
    deleted, detail = reaper.reap(CONFIG, tmp_path)
    assert deleted == 0
    assert "hunter2" not in detail


def test_a_contract_that_disables_reaping_deletes_nothing(tmp_path: Path) -> None:
    rules = RULES.model_copy(update={"enabled": False, "min_empty_age_seconds": 0})
    broker, clock = Broker({LEDGER: "EMPTY"}), [0.0]
    reaper = _reaper(broker, clock, rules)
    reaper.reap(CONFIG, tmp_path)
    assert reaper.reap(CONFIG, tmp_path)[0] == 0


def test_the_tick_reaps_and_reports_in_the_census(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnimarket.nodes.node_consumer_group_guard_effect.__main__ import main
    from omnimarket.nodes.node_consumer_group_guard_effect.handlers import (
        handler_consumer_group_guard as guard,
    )

    broker, clock = Broker({LEDGER: "EMPTY"}), [0.0]
    monkeypatch.setattr(
        runtime, "HandlerConsumerGroupReaper", lambda: _reaper(broker, clock)
    )
    monkeypatch.setattr(reaper_module, "resolve_broker", lambda _l: ("b", "local", {}))
    monkeypatch.setattr(guard, "resolve_broker", lambda _l: ("b", "local", {}))
    monkeypatch.setattr(guard, "read_group_states", lambda _c: {"EMPTY": 1})
    tick = ["--bus-lane", "dev", "--state-dir", str(tmp_path / "state")]
    assert main(tick) == 0
    clock[0] = 600
    assert main(tick) == 0
    census = (tmp_path / "state" / "census.json").read_text(encoding="utf-8")
    assert '"groups_reaped": 1' in census
    assert broker.deleted == [LEDGER]
