# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 2026-10-19 replay: a cause held on a red base, ten hours, and no alarm.

The landing controller refused a private plugin repository's cause every tick with
``cause-held-same-base ... no worker until it moves`` after an attempt that
never ran, and nothing alerted. The timeline is reconstructed: the dispatch and
the blocked release are the cause worker's ledger rows (L11542, 17:04:57Z and
18:25:14Z), the hold repeats on the controller's 300 s tick for about ten hours
as the brief states, and the merge that moves it is placed at the end of that
hold. The replay runs the controller's transitions through the fold, the
watchdog compute once a minute and the delivery effect against a fake channel:
the episode opens past the 60 minute bound, one alarm reaches the channel
naming the cause, the repository, the minutes and the last decision text, and
the episode clears after the merge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.models.liveness.model_automation_liveness import (
    EnumAutomationLivenessVerdict,
    load_automation_liveness_overlay,
)
from omnimarket.models.liveness.model_landing_stall import (
    EnumLandingStallSubject,
    EnumLandingStallTransition,
    ModelLandingStallTransition,
)
from omnimarket.nodes.node_automation_liveness_watchdog_compute.models.model_automation_liveness_watchdog import (
    ModelObserverFacts,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.handlers.handler_landing_stall_expectation import (
    HandlerLandingStallExpectation,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_fold import (
    ModelLandingStallFoldRequest,
    ModelLandingStallRoute,
)
from tests.nodes.node_automation_liveness_watchdog_compute.support import Driver
from tests.nodes.node_automation_liveness_watchdog_effect.support import (
    FakeChannel,
    make_handler,
    tick,
)

OVERLAY_PATH = (
    Path(__file__).resolve().parents[2]
    / "fixtures/automation_liveness/replay_2026_10_19/overlay.yaml"
)
CAUSE_PROCESS = "host-a/landing-cause-stall"
BUMP_PROCESS = "host-a/landing-version-bump-stall"
HOST = "host-a"
REPO = "example-owner/private-plugin-repo"
CAUSE = "cause:example-owner/private-plugin-repo:5d339644b9fa"
BASE = "64989895fe"
DISPATCHED_AT = datetime(2026, 10, 19, 17, 4, 57, tzinfo=UTC)
NEVER_RAN_AT = datetime(2026, 10, 19, 18, 25, 14, tzinfo=UTC)
HOLD = timedelta(hours=10)
MERGED_AT = NEVER_RAN_AT + HOLD
TICK = timedelta(seconds=300)
HELD_TEXT = (
    f"cause-held-same-base: the last attempt (landing-cause-L11542 "
    f"{NEVER_RAN_AT.isoformat()} blocked) failed on base {BASE}; "
    "no worker until it moves"
)
ROUTES = (
    ModelLandingStallRoute(
        subject=EnumLandingStallSubject.CAUSE, process_id=CAUSE_PROCESS, host=HOST
    ),
    ModelLandingStallRoute(
        subject=EnumLandingStallSubject.BASE_BRANCH,
        process_id=CAUSE_PROCESS,
        host=HOST,
    ),
    ModelLandingStallRoute(
        subject=EnumLandingStallSubject.VERSION_BUMP,
        process_id=BUMP_PROCESS,
        host=HOST,
    ),
)


def _t(
    transition: EnumLandingStallTransition, at: datetime, text: str
) -> ModelLandingStallTransition:
    return ModelLandingStallTransition(
        subject=EnumLandingStallSubject.CAUSE,
        transition=transition,
        repo=REPO,
        subject_key=CAUSE,
        decision_text=text,
        observed_at=at,
    )


def timeline() -> list[ModelLandingStallTransition]:
    events = [
        _t(EnumLandingStallTransition.DISPATCHED, DISPATCHED_AT, "dispatch_worker"),
        _t(
            EnumLandingStallTransition.ATTEMPT_NEVER_RAN,
            NEVER_RAN_AT,
            "cause worker landing-cause-L11542 released blocked: no lab capacity",
        ),
    ]
    at = NEVER_RAN_AT + TICK
    while at < MERGED_AT:
        events.append(_t(EnumLandingStallTransition.HELD, at, HELD_TEXT))
        at += TICK
    events.append(_t(EnumLandingStallTransition.MERGED, MERGED_AT, "merged"))
    return events


def replay(tmp_path: Path) -> tuple[Driver, FakeChannel, list[datetime]]:
    driver = Driver(load_automation_liveness_overlay(OVERLAY_PATH))
    channel = FakeChannel()
    effect = make_handler(tmp_path, channel)
    fold = HandlerLandingStallExpectation()
    events = timeline()
    sent_at: list[datetime] = []
    now = DISPATCHED_AT
    end = MERGED_AT + timedelta(minutes=30)
    while now <= end:
        seen = tuple(e for e in events if e.observed_at <= now)
        expectations = fold.handle(
            ModelLandingStallFoldRequest(transitions=seen, routes=ROUTES)
        ).expectations
        result = driver.tick(
            now,
            expectations=expectations,
            observers=(
                ModelObserverFacts(
                    host=HOST,
                    observer_process_id=f"{HOST}/observer",
                    expected_since=DISPATCHED_AT - timedelta(days=1),
                    last_heartbeat_at=now - timedelta(seconds=30),
                ),
            ),
        )
        before = len(channel.messages)
        tick(effect, now, *result.alarms_raised)
        sent_at.extend([now] * (len(channel.messages) - before))
        now += timedelta(seconds=60)
    return driver, channel, sent_at


@pytest.mark.unit
def test_landing_stall_replay_2026_10_19_opens_alerts_once_and_clears(
    tmp_path: Path,
) -> None:
    driver, channel, sent_at = replay(tmp_path)

    raised = [r for r in driver.raised if r.process_id == CAUSE_PROCESS]
    assert len(raised) == 1, [r.model_dump(mode="json") for r in driver.raised]
    episode = raised[0]
    assert episode.verdict is EnumAutomationLivenessVerdict.TRIGGER_UNANSWERED
    # Past the 60 minute bound, and inside the entry's alarm deadline.
    assert NEVER_RAN_AT + timedelta(minutes=60) <= episode.opened_at
    assert episode.opened_at <= NEVER_RAN_AT + timedelta(seconds=4020)

    # One message, naming the cause, the repository, how long, the last decision.
    assert len(channel.messages) == 1
    text = channel.messages[0][1]
    assert sent_at[0] == episode.opened_at
    assert CAUSE in text
    assert REPO in text
    assert "(60 min)" in text or "(61 min)" in text
    assert f"last decision: held: {HELD_TEXT}" in text
    assert str(episode.episode_id) in text

    cleared = [c for c in driver.cleared if c.process_id == CAUSE_PROCESS]
    assert len(cleared) == 1
    assert cleared[0].episode_id == episode.episode_id
    assert MERGED_AT < cleared[0].cleared_at <= MERGED_AT + timedelta(minutes=30)
    assert not [r for r in driver.raised if r.process_id == BUMP_PROCESS]


def summary(tmp_path: Path) -> list[str]:
    driver, channel, _ = replay(tmp_path)
    lines = [f"raised={len(driver.raised)} cleared={len(driver.cleared)}"]
    lines += [
        f"raised {r.opened_at:%m-%dT%H:%MZ} {r.process_id} {r.verdict.value}"
        for r in driver.raised
    ]
    lines += [
        f"cleared {c.cleared_at:%m-%dT%H:%MZ} {c.process_id}" for c in driver.cleared
    ]
    lines += [f"alert: {text}" for _, text in channel.messages]
    return lines


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        for line in summary(Path(d)):
            print(line)
