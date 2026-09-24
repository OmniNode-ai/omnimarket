# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily delegate-skill command-to-terminal join (OMN-19440).

``delegation_events`` keys on correlation_id and upserts, so a second terminal
overwrites the first and the table can never show a duplicate, and nothing
joins a published command to its terminals. ``scripts/delegation_terminal_join.py``
reads the command topic and the terminal topics that the orchestrator's contract
declares, and reports the share of correlation ids that got a terminal and the
count that got more than one.

The two acceptance criteria are pinned here:

* AC1: a planted command with no terminal and a planted double terminal are both
  reported, both on a synthetic day and through the run path the workflow uses.
* AC2: an unreadable topic makes the result indeterminate, with no share and no
  counts, never a zero.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
CONTRACT_PATH = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

HOUR_MS = 3_600_000
NOW_MS = 1_790_280_000_000  # 2026-09-24T19:20:00Z
GRACE_MS = 900_000


def _load(module_name: str) -> types.ModuleType:
    """Load a scripts/ module the way `python scripts/<name>.py` would."""
    scripts_dir = str(SCRIPTS_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / f"{module_name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def join() -> types.ModuleType:
    return _load("delegation_terminal_join")


def _declared_topics() -> tuple[str, list[str]]:
    """Read the topics straight from the contract, the same source the script reads."""
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    dispatch = contract["runtime_dispatch"]
    return dispatch["command_topic"], sorted(dispatch["terminal_events"].values())


def _command(cid: str, ts_ms: int) -> bytes:
    """A command record as the CLI publishes it: correlation_id at the top level."""
    return json.dumps({"prompt": "say ok", "correlation_id": cid}).encode()


def _terminal(cid: str) -> bytes:
    """A terminal record as the runtime publishes it: an envelope around the payload."""
    return json.dumps(
        {
            "correlation_id": cid,
            "event_type": "omnimarket.delegate-skill-completed",
            "payload": {"status": "completed", "correlation_id": cid},
        }
    ).encode()


def _day(join: types.ModuleType) -> tuple[object, dict[str, object]]:
    """A synthetic day: 4 commands, one with no terminal, one with two terminals."""
    command_topic, terminal_topics = _declared_topics()
    window = join.window_for(now_ms=NOW_MS, window_hours=24, grace_ms=GRACE_MS)
    t0 = window.commands_from_ms + HOUR_MS
    commands = join.TopicRead(
        topic=command_topic,
        records=[
            join.Record(t0, "c-ok-1"),
            join.Record(t0 + 1, "c-ok-2"),
            join.Record(t0 + 2, "c-dropped"),
            join.Record(t0 + 3, "c-double"),
        ],
    )
    completed = join.TopicRead(
        topic=terminal_topics[0],
        records=[
            join.Record(t0 + 10, "c-ok-1"),
            join.Record(t0 + 11, "c-double"),
        ],
    )
    failed = join.TopicRead(
        topic=terminal_topics[1],
        records=[
            join.Record(t0 + 12, "c-ok-2"),
            join.Record(t0 + 13, "c-double"),
        ],
    )
    return window, {"commands": commands, "terminals": [completed, failed]}


class TestTopicsComeFromTheContract:
    def test_reads_the_declared_command_and_terminal_topics(
        self, join: types.ModuleType
    ) -> None:
        command_topic, terminal_topics = _declared_topics()
        declared = join.load_declared_topics(CONTRACT_PATH)
        assert declared.command == command_topic
        assert sorted(declared.terminals) == terminal_topics
        assert declared.max_timeout_ms > 0

    def test_refuses_a_contract_without_runtime_dispatch(
        self, join: types.ModuleType, tmp_path: Path
    ) -> None:
        stand_in = tmp_path / "contract.yaml"
        stand_in.write_text("name: stand_in\n", encoding="utf-8")
        with pytest.raises(join.DeclaredTopicsError, match="runtime_dispatch"):
            join.load_declared_topics(stand_in)


class TestCorrelationIdExtraction:
    def test_command_shape_top_level(self, join: types.ModuleType) -> None:
        assert join.extract_correlation_id(_command("abc", 0)) == "abc"

    def test_terminal_envelope_shape(self, join: types.ModuleType) -> None:
        raw = json.dumps({"payload": {"correlation_id": "xyz"}}).encode()
        assert join.extract_correlation_id(raw) == "xyz"

    def test_unparseable_record_has_no_id(self, join: types.ModuleType) -> None:
        assert join.extract_correlation_id(b"not json") is None
        assert join.extract_correlation_id(None) is None


class TestAC1PlantedDropAndDoubleAreReported:
    def test_synthetic_day_reports_both(self, join: types.ModuleType) -> None:
        window, reads = _day(join)
        result = join.compute_join(
            window=window, commands=reads["commands"], terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_MEASURED
        assert result.correlation_ids == 4
        assert result.correlation_ids_with_terminal == 3
        assert result.terminal_share == pytest.approx(0.75)
        assert result.dropped_ids == ["c-dropped"]
        assert result.duplicate_terminal_ids == ["c-double"]
        assert result.duplicate_terminals_with_single_command == 1

    def test_command_inside_the_grace_period_is_not_counted_dropped(
        self, join: types.ModuleType
    ) -> None:
        window, reads = _day(join)
        commands = reads["commands"]
        late = join.Record(window.commands_to_ms + 1, "c-still-running")
        commands = join.TopicRead(
            topic=commands.topic, records=[*commands.records, late]
        )
        result = join.compute_join(
            window=window, commands=commands, terminals=reads["terminals"]
        )
        assert "c-still-running" not in result.dropped_ids
        assert result.correlation_ids == 4

    def test_planted_controls_are_found_and_removed_from_the_live_numbers(
        self, join: types.ModuleType
    ) -> None:
        window, reads = _day(join)
        result = join.measure_with_planted_controls(
            window=window, commands=reads["commands"], terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_MEASURED
        assert result.planted_dropped_detected is True
        assert result.planted_duplicate_detected is True
        # The live numbers exclude the plants.
        assert result.correlation_ids == 4
        assert result.dropped_ids == ["c-dropped"]
        assert result.duplicate_terminal_ids == ["c-double"]

    def test_a_join_that_misses_the_plants_is_indeterminate(
        self, join: types.ModuleType, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, reads = _day(join)
        real = join.compute_join

        def blind(**kwargs: object) -> object:
            result = real(**kwargs)
            result.dropped_ids = []
            result.duplicate_terminal_ids = []
            return result

        monkeypatch.setattr(join, "compute_join", blind)
        result = join.measure_with_planted_controls(
            window=window, commands=reads["commands"], terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_INDETERMINATE
        assert result.terminal_share is None
        assert any("planted" in reason for reason in result.indeterminate_reasons)


class TestAC2UnreadableIsIndeterminateNeverZero:
    def test_unreadable_terminal_topic(self, join: types.ModuleType) -> None:
        window, reads = _day(join)
        broken = join.TopicRead(
            topic=reads["terminals"][1].topic,
            records=[],
            error="KafkaConnectionError: broker closed the connection",
        )
        result = join.measure_with_planted_controls(
            window=window,
            commands=reads["commands"],
            terminals=[reads["terminals"][0], broken],
        )
        assert result.verdict == join.VERDICT_INDETERMINATE
        assert result.terminal_share is None
        assert result.correlation_ids is None
        assert result.dropped_ids is None
        assert result.duplicate_terminal_ids is None
        assert any(broken.topic in reason for reason in result.indeterminate_reasons)

    def test_unreadable_command_topic(self, join: types.ModuleType) -> None:
        window, reads = _day(join)
        broken = join.TopicRead(
            topic=reads["commands"].topic, records=[], error="topic not found"
        )
        result = join.measure_with_planted_controls(
            window=window, commands=broken, terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_INDETERMINATE
        assert result.terminal_share is None

    def test_retention_truncated_window_is_indeterminate(
        self, join: types.ModuleType
    ) -> None:
        window, reads = _day(join)
        truncated = join.TopicRead(
            topic=reads["commands"].topic,
            records=reads["commands"].records,
            window_may_be_truncated=True,
        )
        result = join.measure_with_planted_controls(
            window=window, commands=truncated, terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_INDETERMINATE
        assert result.terminal_share is None

    def test_an_empty_day_is_no_commands_not_a_zero_share(
        self, join: types.ModuleType
    ) -> None:
        window, reads = _day(join)
        empty = join.TopicRead(topic=reads["commands"].topic, records=[])
        result = join.measure_with_planted_controls(
            window=window, commands=empty, terminals=reads["terminals"]
        )
        assert result.verdict == join.VERDICT_NO_COMMANDS
        assert result.terminal_share is None
        assert result.correlation_ids == 0


class TestRunPath:
    """The path the workflow runs: reader -> measure -> JSON file -> exit code."""

    def _reader(self, join: types.ModuleType, fail_topic: str | None = None) -> object:
        command_topic, terminal_topics = _declared_topics()
        t0 = NOW_MS - 20 * HOUR_MS
        data = {
            command_topic: [
                (t0, _command("r-ok", t0)),
                (t0 + 1, _command("r-dropped", t0)),
                (t0 + 2, _command("r-double", t0)),
            ],
            terminal_topics[0]: [(t0 + 10, _terminal("r-ok"))],
            terminal_topics[1]: [
                (t0 + 11, _terminal("r-double")),
                (t0 + 12, _terminal("r-double")),
            ],
        }

        async def reader(topic: str, from_ms: int, to_ms: int) -> object:
            if topic == fail_topic:
                raise ConnectionError("SASL authentication failed")
            return join.TopicRead(
                topic=topic,
                records=[
                    join.Record(ts, join.extract_correlation_id(value))
                    for ts, value in data[topic]
                    if from_ms <= ts <= to_ms
                ],
            )

        return reader

    def test_measured_run_writes_the_result_and_exits_zero(
        self, join: types.ModuleType, tmp_path: Path
    ) -> None:
        out = tmp_path / "result.json"
        code = join.run(
            reader=self._reader(join),
            contract_path=CONTRACT_PATH,
            now_ms=NOW_MS,
            window_hours=24,
            lane="dev",
            out_path=out,
        )
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert code == 0
        assert payload["ticket"] == "OMN-19440"
        assert payload["verdict"] == "measured"
        assert payload["dropped"]["ids"] == ["r-dropped"]
        assert payload["duplicate_terminals"]["ids"] == ["r-double"]
        assert payload["terminal_share"] == pytest.approx(2 / 3)
        assert payload["planted_controls"] == {
            "dropped_detected": True,
            "duplicate_detected": True,
        }

    def test_a_reader_failure_is_indeterminate_and_exits_nonzero(
        self, join: types.ModuleType, tmp_path: Path
    ) -> None:
        _, terminal_topics = _declared_topics()
        out = tmp_path / "result.json"
        code = join.run(
            reader=self._reader(join, fail_topic=terminal_topics[1]),
            contract_path=CONTRACT_PATH,
            now_ms=NOW_MS,
            window_hours=24,
            lane="dev",
            out_path=out,
        )
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert code == join.EXIT_INDETERMINATE
        assert payload["verdict"] == "indeterminate"
        assert payload["terminal_share"] is None
        assert payload["dropped"]["count"] is None
        assert payload["duplicate_terminals"]["count"] is None
        assert "SASL authentication failed" in json.dumps(
            payload["indeterminate_reasons"]
        )
