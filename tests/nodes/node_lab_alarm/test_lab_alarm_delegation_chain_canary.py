# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation-chain-canary condition of node_lab_alarm and its seam to the alarm delivery.

Golden: a fresh, green run reads OK and the event chain is started then completed; a red or stale run
that reaches the overlay's persistence bound alarms, is delivered once through the delivery double, and
the next OK ends the episode. Error: an unreadable source is INDETERMINATE, never ALARM and never OK;
a failed delivery exits as an alarm and names delivery-failed; the failed terminal event is published on
a breach. Failing control: the same run with the bound raised past its age reads OK, and with the bound
as declared reads ALARM, so the bound in the overlay is what alarms; persist_runs 3 gives OK on runs 1
and 2 and ALARM on run 3, and a good read in between resets the streak. Seam: the alarm line a run
produces, split at the first ': ', builds a valid ModelLabAlarm accepted by the real
HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's events_for emits is
declared in the contract's topics and externally_consumed_topics. The overlay loads without the block,
the verb refuses an overlay without the block, the CLI exit codes follow the outcome, and the
in-memory backend builds a handler whose read is INDETERMINATE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import ModelLabAlarm
from omnimarket.nodes.node_lab_alarm.adapters.inmemory_adapters import (
    InMemoryCanaryRunSource,
    InMemoryGrowthStreaks,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_delegation_chain_canary import (
    CONDITION,
    HandlerDelegationChainCanary,
    events_for,
    load_topics,
)
from omnimarket.nodes.node_lab_alarm.models.model_lab_alarm_delegation_chain_canary import (
    EnumDelegationChainCanaryFault,
    EnumDelegationChainCanaryOutcome,
    ModelCanaryRun,
    ModelDelegationChainCanaryCommand,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelConditionPersistence,
    ModelDelegationChainCanarySettings,
    ModelLabAlarmOverlay,
)
from omnimarket.nodes.node_lab_alarm.runtime_lab_alarm import (
    build_delegation_chain_canary_handler,
    run_delegation_chain_canary,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes/node_lab_alarm"
_DELIVER = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CLOCK = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)
_REPO = "example-org/example-repo"
_WORKFLOW = "example-canary.yml"
_BRANCH = "main"


def _settings(
    repository: str = _REPO,
    workflow: str = _WORKFLOW,
    branch: str = _BRANCH,
    events: tuple[str, ...] = ("schedule",),
    max_age_seconds: float = 10800,
    persist_runs: int = 1,
) -> ModelDelegationChainCanarySettings:
    return ModelDelegationChainCanarySettings(
        repository=repository,
        workflow=workflow,
        branch=branch,
        events=events,
        max_age_seconds=max_age_seconds,
        per_page=20,
        timeout_seconds=45,
        persistence=ModelConditionPersistence(
            persist_runs=persist_runs,
            streak_path=".onex_state/lab-alarm/delegation-chain-canary-streaks.json",
        ),
    )


def _command(
    *,
    write: bool = True,
    fault: EnumDelegationChainCanaryFault = EnumDelegationChainCanaryFault.NONE,
) -> ModelDelegationChainCanaryCommand:
    return ModelDelegationChainCanaryCommand(
        correlation_id="canary-1", write=write, fault=fault
    )


def _green_run() -> ModelCanaryRun:
    return ModelCanaryRun(
        id=1,
        event="schedule",
        status="completed",
        conclusion="success",
        created_at=_CLOCK,
    )


def _red_run() -> ModelCanaryRun:
    return ModelCanaryRun(
        id=2,
        event="schedule",
        status="completed",
        conclusion="failure",
        created_at=_CLOCK,
    )


def _stale_run() -> ModelCanaryRun:
    return ModelCanaryRun(
        id=3,
        event="schedule",
        status="completed",
        conclusion="success",
        created_at=_CLOCK.replace(hour=0, minute=0),
    )


class _Rig:
    def __init__(
        self,
        runs: tuple[ModelCanaryRun, ...] = (),
        *,
        settings: ModelDelegationChainCanarySettings | None = None,
        deliver: bool = True,
    ) -> None:
        self.source = InMemoryCanaryRunSource(runs=runs)
        self.streaks = InMemoryGrowthStreaks()
        self.channel = InMemoryAlarmChannel()
        self.ledger = InMemoryAlarmLedger()
        delivery = HandlerLabAlarmDeliver(
            overlay=ModelDeliveryOverlay.model_validate(
                yaml.safe_load(
                    (_DELIVER / "overlay.example.yaml").read_text(encoding="utf-8")
                )
            ),
            channel=self.channel,
            ledger=self.ledger,
            state=InMemoryDeliveryState(),
            clock=lambda: _CLOCK,
        )
        self.handler = HandlerDelegationChainCanary(
            settings=settings if settings is not None else _settings(),
            source=self.source,
            streaks=self.streaks,
            delivery=delivery if deliver else None,
            clock=lambda: _CLOCK,
        )


def test_delegation_chain_canary_golden_chain() -> None:
    """A green, fresh run reads OK and the event chain is started then completed; a red run that
    reaches persist_runs alarms, delivers once through the delivery double and the next OK ends the
    episode."""
    topics = load_topics()
    rig = _Rig(runs=(_green_run(),))
    golden = rig.handler.handle(_command())
    assert golden.outcome is EnumDelegationChainCanaryOutcome.OK
    assert golden.alarms == ()
    assert golden.reasons == ()
    assert golden.failure == ""
    assert [t for t, _ in events_for(golden, topics)] == [
        topics["delegation_chain_canary_run_started"],
        topics["delegation_chain_canary_run_completed"],
    ]
    rig.source.runs = (_red_run(),)
    rig.source.error = None
    breach = rig.handler.handle(_command())
    assert breach.outcome is EnumDelegationChainCanaryOutcome.ALARM
    [alarm] = breach.alarms
    assert alarm.startswith("delegation-chain-canary:")
    assert "run 2 concluded failure" in alarm
    assert breach.delivery == (
        f"{CONDITION}:delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH} channel=delivered ledger=delivered",
    )
    rig.source.runs = (_green_run(),)
    rig.source.error = None
    ok = rig.handler.handle(_command())
    assert ok.outcome is EnumDelegationChainCanaryOutcome.OK
    assert rig.streaks.streaks == {}, "the OK run cleared the streak"


def test_delegation_chain_canary_error_chain() -> None:
    """Unreadable source is INDETERMINATE, never ALARM or OK; a failed delivery exits 2 and names
    delivery-failed; the failed terminal event is published on a breach."""
    topics = load_topics()
    rig = _Rig(runs=(_green_run(),))
    rig.source.error = "connection refused"
    unreadable = rig.handler.handle(_command())
    assert unreadable.outcome is EnumDelegationChainCanaryOutcome.INDETERMINATE
    assert unreadable.reasons == ("canary-runs-unreadable",)
    assert unreadable.alarms == ()
    assert unreadable.failure == "indeterminate:canary-runs-unreadable"
    assert [t for t, _ in events_for(unreadable, topics)][-1] == topics[
        "delegation_chain_canary_run_failed"
    ]
    rig2 = _Rig(runs=(_red_run(),))
    rig2.ledger.refuse = "ledger host unreachable"
    failed = rig2.handler.handle(_command())
    assert "delivery-failed" in failed.failure
    assert "ledger host unreachable" in failed.failure
    assert [t for t, _ in events_for(failed, topics)][-1] == topics[
        "delegation_chain_canary_run_failed"
    ]


def test_delegation_chain_canary_failing_control() -> None:
    """The same run with max_age_seconds raised past its age reads OK, and with the bound as declared
    reads ALARM, so the bound in the overlay is what alarms; persist_runs 3 gives OK on runs 1 and 2
    and ALARM on run 3 while a good read in between resets the streak."""
    rig = _Rig(runs=(_stale_run(),), settings=_settings(max_age_seconds=86400))
    assert rig.handler.handle(_command()).outcome is EnumDelegationChainCanaryOutcome.OK
    rig2 = _Rig(runs=(_stale_run(),), settings=_settings(max_age_seconds=10800))
    assert (
        rig2.handler.handle(_command()).outcome
        is EnumDelegationChainCanaryOutcome.ALARM
    )
    rig3 = _Rig(runs=(_red_run(),), settings=_settings(persist_runs=3))
    first = rig3.handler.handle(_command())
    assert first.outcome is EnumDelegationChainCanaryOutcome.OK
    assert first.reasons == (
        f"waiting:delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH}: run 1 of 3",
    )
    second = rig3.handler.handle(_command())
    assert second.outcome is EnumDelegationChainCanaryOutcome.OK
    assert second.reasons == (
        f"waiting:delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH}: run 2 of 3",
    )
    rig3.source.runs = (_green_run(),)
    rig3.source.error = None
    reset = rig3.handler.handle(_command())
    assert reset.outcome is EnumDelegationChainCanaryOutcome.OK
    assert reset.reasons == ()
    assert rig3.streaks.streaks == {}, "the good read cleared the streak"
    rig3.source.runs = (_red_run(),)
    rig3.source.error = None
    rig3.handler.handle(_command())
    rig3.handler.handle(_command())
    third = rig3.handler.handle(_command())
    assert third.outcome is EnumDelegationChainCanaryOutcome.ALARM
    assert third.reasons == ()


def test_delegation_chain_canary_seam() -> None:
    """The alarm line a run produces, split at the first ': ', builds a valid ModelLabAlarm accepted by
    the real HandlerLabAlarmDeliver with the in-memory backend; every topic name the handler's
    events_for emits is declared in the contract's topics and externally_consumed_topics."""
    rig = _Rig(runs=(_red_run(),))
    result = rig.handler.handle(_command())
    [alarm] = result.alarms
    subject, separator, detail = alarm.partition(": ")
    assert separator == ": "
    assert subject == f"delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH}"
    model = ModelLabAlarm(condition=CONDITION, subject=subject, detail=detail)
    assert model.condition == CONDITION
    assert model.subject == subject
    assert model.detail == detail
    topics = load_topics()
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text(encoding="utf-8"))
    declared = set(contract["topics"])
    external = set(contract.get("externally_consumed_topics", []))
    emitted = {t for t, _ in events_for(result, topics)}
    assert {k for k, v in topics.items() if v in emitted} <= declared
    assert emitted <= set(contract["topics"].values())
    assert emitted <= external, "every emitted topic is declared as externally consumed"


def _example_raw() -> dict[str, object]:
    raw = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def test_delegation_chain_canary_overlay_loads_without_the_block() -> None:
    raw = _example_raw()
    assert ModelLabAlarmOverlay.model_validate(raw).delegation_chain_canary is not None
    del raw["delegation_chain_canary"]
    assert ModelLabAlarmOverlay.model_validate(raw).delegation_chain_canary is None


def test_delegation_chain_canary_verb_refuses_an_overlay_without_the_block(
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw = _example_raw()
    del raw["delegation_chain_canary"]
    code = run_delegation_chain_canary(
        "delegation-chain-canary",
        EnumDelegationChainCanaryFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(raw),
    )
    assert code == 1
    assert "no delegation_chain_canary block" in capsys.readouterr().err


def test_delegation_chain_canary_cli_exit_codes_follow_the_outcome() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    rig = _Rig(runs=(_red_run(),))
    lines: list[str] = []
    assert (
        run_delegation_chain_canary(
            "delegation-chain-canary-probe",
            EnumDelegationChainCanaryFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 2
    )
    assert any(
        line.startswith("DELEGATION-CHAIN-CANARY outcome=ALARM") for line in lines
    )
    assert any(
        line.startswith(f"ALARM delegation-chain-canary:{_REPO}") for line in lines
    )
    assert rig.streaks.streaks == {}, "a probe keeps no streak"
    assert rig.channel.posts == [], "a probe delivers nothing"
    rig.source.runs = (_green_run(),)
    rig.source.error = None
    assert (
        run_delegation_chain_canary(
            "delegation-chain-canary",
            EnumDelegationChainCanaryFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 0
    )
    rig.source.error = "connection refused"
    assert (
        run_delegation_chain_canary(
            "delegation-chain-canary",
            EnumDelegationChainCanaryFault.NONE,
            overlay=overlay,
            handler=rig.handler,
            out=lines.append,
        )
        == 3
    )


def test_delegation_chain_canary_cli_exits_alarm_when_the_delivery_fails() -> None:
    rig = _Rig(runs=(_red_run(),))
    rig.ledger.refuse = "ledger host unreachable"
    code = run_delegation_chain_canary(
        "delegation-chain-canary",
        EnumDelegationChainCanaryFault.NONE,
        overlay=ModelLabAlarmOverlay.model_validate(_example_raw()),
        handler=rig.handler,
        out=lambda _: None,
    )
    assert code == 2


def test_delegation_chain_canary_a_drill_alarms_without_reading_keeping_or_delivering() -> (
    None
):
    rig = _Rig(runs=(_green_run(),))
    result = rig.handler.handle(
        _command(fault=EnumDelegationChainCanaryFault.RED_CANARY)
    )
    assert result.outcome is EnumDelegationChainCanaryOutcome.ALARM
    assert rig.streaks.streaks == {}
    assert rig.channel.posts == []
    assert result.delivery == ()


def test_delegation_chain_canary_a_probe_keeps_no_streak_and_a_write_does() -> None:
    rig = _Rig(runs=(_red_run(),), settings=_settings(persist_runs=3))
    rig.handler.handle(_command(write=False))
    assert rig.streaks.streaks == {}
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {
        f"delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH}": 1
    }
    rig.source.error = "connection refused"
    rig.handler.handle(_command())
    assert rig.streaks.streaks == {
        f"delegation-chain-canary:{_REPO} {_WORKFLOW}@{_BRANCH}": 1
    }, "an unreadable run leaves the streak"


def test_delegation_chain_canary_newest_wins_in_both_directions() -> None:
    """A red re-run supersedes an earlier green, and a green one an earlier red; order in the list is irrelevant."""
    earlier = _CLOCK - timedelta(hours=1)
    green, red = _green_run(), _red_run()
    old_green = green.model_copy(update={"created_at": earlier})
    old_red = red.model_copy(update={"created_at": earlier})
    for runs in ((old_green, red), (red, old_green)):
        result = _Rig(runs=runs).handler.handle(_command())
        assert result.outcome is EnumDelegationChainCanaryOutcome.ALARM
    for runs in ((old_red, green), (green, old_red)):
        result = _Rig(runs=runs).handler.handle(_command())
        assert result.outcome is EnumDelegationChainCanaryOutcome.OK


def test_delegation_chain_canary_inmemory_backend_builds_the_fixture_source() -> None:
    overlay = ModelLabAlarmOverlay.model_validate(_example_raw())
    assert overlay.delegation_chain_canary is not None
    handler = build_delegation_chain_canary_handler(
        overlay, overlay.delegation_chain_canary, lambda: _CLOCK
    )
    result = handler.handle(_command(write=False))
    assert result.outcome is EnumDelegationChainCanaryOutcome.INDETERMINATE
    assert result.reasons == ("canary-runs-unreadable",)
