# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19440 acceptance coverage through the scheduled job's publication path."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import delegation_terminal_join as join

pytestmark = pytest.mark.unit

NOW_MS = 1_790_280_000_000
T0 = NOW_MS - 12 * 3_600_000


def _publish(
    tmp_path: Path,
    commands: list[join.Record],
    completed: list[join.Record],
    failed: list[join.Record],
    unreadable: int | None = None,
) -> tuple[int, dict[str, Any], str]:
    topics = join.load_declared_topics()
    names = [topics.command, *topics.terminals]
    records = dict(zip(names, [commands, completed, failed], strict=True))

    async def reader(topic: str, from_ms: int, to_ms: int) -> join.TopicRead:
        if unreadable is not None and topic == names[unreadable]:
            raise ConnectionError("topic read failed")
        return join.TopicRead(
            topic=topic,
            records=[
                record
                for record in records[topic]
                if from_ms <= record.timestamp_ms <= to_ms
            ],
        )

    output = tmp_path / "result.json"
    summary = tmp_path / "summary.md"
    code = join.run(
        reader=reader,
        contract_path=join.CONTRACT_PATH,
        now_ms=NOW_MS,
        window_hours=24,
        lane="dev",
        out_path=output,
        summary_path=summary,
    )
    return code, json.loads(output.read_text()), summary.read_text()


@pytest.mark.parametrize("reverse", [False, True])
def test_ac1_publishes_drop_and_cross_topic_double(
    tmp_path: Path, reverse: bool
) -> None:
    completed = [join.Record(T0 + 10, "ok"), join.Record(T0 + 11, "double")]
    failed = [join.Record(T0 + 12, "double")]
    if reverse:
        completed, failed = failed, completed
    code, payload, summary = _publish(
        tmp_path,
        [join.Record(T0, cid) for cid in ("ok", "dropped", "double")],
        completed,
        failed,
    )
    assert code == join.EXIT_OK
    assert payload["verdict"] == "measured"
    assert payload["terminal_share"] == pytest.approx(2 / 3)
    assert payload["commands"]["records"] == 3
    assert payload["dropped"] == {"count": 1, "ids": ["dropped"]}
    assert payload["duplicate_terminals"]["ids"] == ["double"]
    assert all(payload["planted_controls"].values())
    assert "- dropped (no terminal): 1" in summary


@pytest.mark.parametrize("unreadable", [0, 1, 2])
def test_ac2_publishes_indeterminate_for_each_unreadable_topic(
    tmp_path: Path, unreadable: int
) -> None:
    code, payload, summary = _publish(
        tmp_path,
        [join.Record(T0, "ok", message_id="command-ok")],
        [join.Record(T0 + 1, "ok", command_id="command-ok", outcome="done")],
        [],
        unreadable=unreadable,
    )
    assert code == join.EXIT_INDETERMINATE
    assert payload["verdict"] == "indeterminate"
    assert payload["terminal_share"] is None
    assert payload["dropped"] == {"count": None, "ids": None}
    assert payload["duplicate_terminals"]["count"] is None
    for name in ("dropped", "second_outcomes", "replays", "reuses"):
        assert payload["per_command"][name] == {"count": None, "ids": None}
    assert any("ConnectionError" in r for r in payload["indeterminate_reasons"])
    assert "- share with a terminal: n/a" in summary
    assert "- verdict: **indeterminate**" in summary


@pytest.mark.parametrize("replay_count", [2, 3])
def test_ac3_publishes_replay_reuse_drop_and_second_outcome_separately(
    tmp_path: Path, replay_count: int
) -> None:
    code, payload, summary = _publish(
        tmp_path,
        [
            join.Record(T0, "replayed", message_id="replay"),
            join.Record(T0, "reused", message_id="served"),
            join.Record(T0 + 1, "reused", message_id="lost"),
            join.Record(T0 + 2, "reused", message_id="second"),
        ],
        [
            *[
                join.Record(T0 + 10 + i, "replayed", command_id="replay", outcome="A")
                for i in range(replay_count)
            ],
            join.Record(T0 + 20, "reused", command_id="served", outcome="A"),
            join.Record(T0 + 21, "reused", command_id="second", outcome="A"),
        ],
        [join.Record(T0 + 22, "reused", command_id="second", outcome="B")],
    )
    assert code == join.EXIT_OK
    per_command = payload["per_command"]
    assert per_command["command_ids"] == 4
    assert per_command["dropped"] == {"count": 1, "ids": ["lost"]}
    assert per_command["second_outcomes"] == {"count": 1, "ids": ["second"]}
    assert per_command["replays"] == {"count": 1, "ids": ["replay"]}
    assert per_command["reuses"] == {"count": 1, "ids": ["reused"]}
    assert per_command["planted_control_detected"] is True
    assert "dropped 1, second outcomes 1, replays 1, reuses 1" in summary


@pytest.mark.parametrize("reused_commands", [2, 3])
def test_ac4_publishes_single_command_headline(
    tmp_path: Path, reused_commands: int
) -> None:
    code, payload, summary = _publish(
        tmp_path,
        [join.Record(T0, "double")]
        + [join.Record(T0 + i, "reused") for i in range(reused_commands)],
        [join.Record(T0 + 10, "double"), join.Record(T0 + 11, "reused")],
        [join.Record(T0 + 12, "double"), join.Record(T0 + 13, "reused")],
    )
    assert code == join.EXIT_OK
    assert payload["duplicate_terminals"]["count"] == 2
    assert payload["duplicate_terminals"]["with_single_command"] == 1
    assert (
        "- duplicate terminals (single command): 1 "
        "[correlation ids with more than one terminal: 2]"
    ) in summary
