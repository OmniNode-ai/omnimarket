# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One alarm episode writes one resolved row (OMN-22868).

On 2026-11-09 the throughput-results alarm raised once at 23:41:37Z and wrote a resolved row for the
episode ``al-20261108T2341Z-throughput-results`` twice: 00:09:02Z (27 minutes open) and 00:16:36Z (34
minutes open). The first resolved row deleted the open alarm from the delivery state. Merges fell
under the bound again a few minutes later while the 23:41:37Z FRICTION row was still inside the
two-hour dedupe window, so the next tick adopted that old row as the open alarm (its resolved row was
never read) and the recovery after it wrote a second resolved row for the same episode id.

The controls below run the same ticks on the unfixed reader: each reading goes red there (the second
one on the resolved row the ledger already holds), so it depends on the rule and not on the fixture.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.silent_failure_alarm import ModelDrainerObservation
from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
    HandlerSilentFailureAlarm,
)
from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
    ModelSilentFailureAlarmOptions,
    ModelSilentFailureAlarmRequest,
)
from tests.nodes.node_silent_failure_alarm_compute.test_golden_chain_silent_failure_alarm_compute import (
    HOST_NOW,
    Watch,
    world,
)

pytestmark = pytest.mark.unit

BOUND = 5
OPEN_PRS = 12
EPISODE = "al-{stamp}-throughput-results"
RAISED = (
    "{stamp} | FRICTION | lane=silent-failure-alarm | ticket=OMN-19998 | actor=launchd:silent-failure-alarm | "
    "class=throughput-results | short=1 | cost=est ~60 minutes of factory output short of its bound | "
    "3 merges in the last hour with 217 open non-draft PRs (bound 5)"
)
RESOLVED = (
    "{stamp} | STATUS | lane=silent-failure-alarm | ticket=OMN-19998 | actor=launchd:silent-failure-alarm | "
    "episode={episode} | resolved=throughput-results | open_minutes={minutes} | "
    "throughput-results alarm resolved after {minutes} minutes open"
)


def _stamp(at) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ledger(options: ModelSilentFailureAlarmOptions) -> Path:
    assert options.ledger is not None
    return options.ledger


def _watcher(options: ModelSilentFailureAlarmOptions, at, merges: int) -> None:
    """The PR watcher state at ``at``: ``merges`` merged in the last hour, OPEN_PRS open non-draft."""
    assert options.watcher_state is not None
    options.watcher_state.write_text(
        json.dumps(
            {
                "last_tick": _stamp(at),
                "prs": {
                    f"omnimarket#{n}": {"facts": {"state": "OPEN", "draft": False}}
                    for n in range(OPEN_PRS)
                },
                "merges": {
                    f"omnimarket#{900 + n}": {
                        "merged_at": _stamp(at - timedelta(minutes=10 + n))
                    }
                    for n in range(merges)
                },
            }
        )
    )


def _tick(options: ModelSilentFailureAlarmOptions, at, merges: int) -> None:
    _watcher(options, at, merges)
    watches = {
        host: Watch(
            ModelDrainerObservation(job_state="not-running", status_readable=False)
        )
        for host in options.host_aliases
    }
    result = HandlerSilentFailureAlarm(options, host_watches=watches).handle(
        ModelSilentFailureAlarmRequest(now=at)
    )
    assert result.exit_code == 0, result.warnings


def _rows(options: ModelSilentFailureAlarmOptions, marker: str) -> list[str]:
    return [
        line
        for line in _ledger(options).read_text().splitlines()
        if marker in line and "lane=silent-failure-alarm" in line
    ]


def _episodes(options: ModelSilentFailureAlarmOptions) -> list[str]:
    return [
        row.split("episode=", 1)[1].split(" | ", 1)[0]
        for row in _rows(options, "resolved=throughput-results")
    ]


def _options(tmp_path: Path) -> ModelSilentFailureAlarmOptions:
    return world(tmp_path).model_copy(update={"min_merges_per_hour": BOUND})


def test_recovery_then_a_new_shortfall_in_the_dedupe_window_is_a_new_episode(
    tmp_path: Path,
) -> None:
    """The 2026-11-09 sequence: raise, recover, fall short again 8 minutes on, recover again."""
    options = _options(tmp_path)
    _tick(options, HOST_NOW, merges=1)
    assert len(_rows(options, "class=throughput-results")) == 1

    _tick(options, HOST_NOW + timedelta(minutes=27), merges=9)
    assert len(_episodes(options)) == 1

    # Short again inside the dedupe window of the first FRICTION row: a new episode, its own row.
    _tick(options, HOST_NOW + timedelta(minutes=35), merges=1)
    assert len(_rows(options, "class=throughput-results")) == 2, (
        "a shortfall after a resolved row is a new episode and raises its own row"
    )

    _tick(options, HOST_NOW + timedelta(minutes=42), merges=9)
    episodes = _episodes(options)
    assert len(episodes) == 2
    assert len(set(episodes)) == 2, f"one resolved row per episode, got {episodes}"
    assert episodes[0] == EPISODE.format(stamp=HOST_NOW.strftime("%Y%m%dT%H%MZ"))


def test_an_episode_the_ledger_already_resolved_is_not_resolved_again(
    tmp_path: Path,
) -> None:
    """The open alarm survives in the state file (or is adopted) after a resolved row exists."""
    options = _options(tmp_path)
    raised_at = HOST_NOW - timedelta(minutes=40)
    resolved_at = HOST_NOW - timedelta(minutes=13)
    episode = EPISODE.format(stamp=raised_at.strftime("%Y%m%dT%H%MZ"))
    with _ledger(options).open("a") as handle:
        handle.write(RAISED.format(stamp=_stamp(raised_at)) + "\n")
        handle.write(
            RESOLVED.format(stamp=_stamp(resolved_at), episode=episode, minutes=27)
            + "\n"
        )
    options.delivery_state_file.write_text(
        json.dumps(
            {
                "pending": {},
                "delivered": {},
                "open": {
                    "throughput-results": {
                        "started": _stamp(raised_at),
                        "escalated": None,
                        "hosts": [],
                    }
                },
            }
        )
    )
    _tick(options, HOST_NOW, merges=9)
    assert _episodes(options) == [episode], (
        "the ledger already holds this episode's resolved row"
    )
    state = json.loads(options.delivery_state_file.read_text())
    assert "throughput-results" not in state["open"]
