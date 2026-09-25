#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Daily read-only join of the delegate-skill command topic to its terminal topics.

Ticket: OMN-19440 (gate OMN-19432 AC-2).

WHY THIS EXISTS. ``delegation_events`` has a unique index on ``correlation_id``
and writes with ``ON CONFLICT DO UPDATE``, so a second terminal for one run
overwrites the first and the table can never show a duplicate. Nothing joined
the published commands to the terminals, so the share of runs that never got a
terminal was unknown. This reads the wire itself and reports:

* the share of command correlation ids that got at least one terminal, and the
  ids that got none (dropped);
* the count of correlation ids that got more than one terminal record
  (duplicates), split by whether the command itself was published once.

WHERE THE TOPICS COME FROM. The command topic, the terminal topics and the
longest run budget are read from ``runtime_dispatch`` in the orchestrator's own
contract (``node_delegate_skill_orchestrator/contract.yaml``). There is no
second list here to drift from it.

THE WINDOW. A command published less than the contract's ``max_timeout_ms``
ago may still be running, so it is not judged: commands are counted from
``now - grace - window`` to ``now - grace``, and terminals are read from the
start of that window up to ``now``.

READ-ONLY BY CONSTRUCTION. The reader is a group-less consumer: it joins no
consumer group, commits nothing and publishes nothing.

PLANTED POSITIVE CONTROLS. Every run adds one synthetic command with no
terminal and one synthetic command with two terminals to the records it read
(in memory only, never on the broker), runs the same join over the combined
set, and checks both plants are reported before it trusts the live numbers. The
plants are then removed from the reported numbers. A join that misses a plant
reports INDETERMINATE.

INDETERMINATE, NEVER ZERO. An unreadable topic, a window the topic's retention
may have cut, or a failed planted control makes the verdict ``indeterminate``,
and every count and the share are ``null``. Exit code 3 turns the scheduled
run red. An empty day is ``no_commands`` with a ``null`` share, not a 0% share.

Usage (on a runner that can reach the lane broker)::

    python scripts/delegation_terminal_join.py --lane dev --out result.json

The broker and its transport come from ``config/ci_bus_lanes.yaml`` via
``ci_bus_lanes``; the SASL principal comes from ``KAFKA_SASL_USERNAME`` /
``KAFKA_SASL_PASSWORD`` in the environment and never reaches argv or a log.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import yaml

TICKET = "OMN-19440"
REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegate_skill_orchestrator"
    / "contract.yaml"
)

VERDICT_MEASURED = "measured"
VERDICT_NO_COMMANDS = "no_commands"
VERDICT_INDETERMINATE = "indeterminate"

EXIT_OK = 0
EXIT_INVOCATION = 2
EXIT_INDETERMINATE = 3

PLANTED_DROPPED_ID = "planted-control-dropped-omn19440"
PLANTED_DUPLICATE_ID = "planted-control-duplicate-omn19440"

MAX_LISTED_IDS = 50
READ_DEADLINE_SECONDS = 120.0


class DeclaredTopicsError(ValueError):
    """The contract does not declare the topics this join needs."""


@dataclass(frozen=True)
class DeclaredTopics:
    command: str
    terminals: tuple[str, ...]
    max_timeout_ms: int


@dataclass(frozen=True)
class Record:
    timestamp_ms: int
    correlation_id: str | None


@dataclass(frozen=True)
class TopicRead:
    topic: str
    records: list[Record]
    error: str | None = None
    window_may_be_truncated: bool = False
    retention_ms: int | None = None
    retention_bytes: int | None = None


@dataclass(frozen=True)
class Window:
    commands_from_ms: int
    commands_to_ms: int
    terminals_to_ms: int
    grace_ms: int


@dataclass
class JoinResult:
    verdict: str
    indeterminate_reasons: list[str] = field(default_factory=list)
    command_records: int | None = None
    command_records_without_correlation_id: int | None = None
    correlation_ids: int | None = None
    correlation_ids_with_repeated_command: int | None = None
    correlation_ids_with_terminal: int | None = None
    terminal_share: float | None = None
    dropped_ids: list[str] | None = None
    duplicate_terminal_ids: list[str] | None = None
    duplicate_terminals_with_single_command: int | None = None
    planted_dropped_detected: bool | None = None
    planted_duplicate_detected: bool | None = None


def load_declared_topics(contract_path: Path = CONTRACT_PATH) -> DeclaredTopics:
    """Read the command topic, terminal topics and run budget from the contract."""
    loaded = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    dispatch = loaded.get("runtime_dispatch") if isinstance(loaded, dict) else None
    if not isinstance(dispatch, dict):
        raise DeclaredTopicsError(
            f"{contract_path} declares no runtime_dispatch block, so it names no "
            "command topic or terminal topics to join"
        )
    command = dispatch.get("command_topic")
    terminal_events = dispatch.get("terminal_events")
    max_timeout_ms = dispatch.get("max_timeout_ms")
    if not isinstance(command, str) or not command:
        raise DeclaredTopicsError(f"{contract_path}: runtime_dispatch.command_topic")
    if not isinstance(terminal_events, dict) or not terminal_events:
        raise DeclaredTopicsError(f"{contract_path}: runtime_dispatch.terminal_events")
    terminals = tuple(sorted(str(topic) for topic in terminal_events.values()))
    if not isinstance(max_timeout_ms, int) or max_timeout_ms <= 0:
        raise DeclaredTopicsError(f"{contract_path}: runtime_dispatch.max_timeout_ms")
    return DeclaredTopics(
        command=command, terminals=terminals, max_timeout_ms=max_timeout_ms
    )


def extract_correlation_id(value: bytes | None) -> str | None:
    """Correlation id of a command (top level) or a terminal envelope (payload)."""
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict):
        return None
    candidate = decoded.get("correlation_id")
    if not candidate:
        payload = decoded.get("payload")
        if isinstance(payload, dict):
            candidate = payload.get("correlation_id")
    return str(candidate) if candidate else None


def window_for(*, now_ms: int, window_hours: int, grace_ms: int) -> Window:
    commands_to_ms = now_ms - grace_ms
    return Window(
        commands_from_ms=commands_to_ms - window_hours * 3_600_000,
        commands_to_ms=commands_to_ms,
        terminals_to_ms=now_ms,
        grace_ms=grace_ms,
    )


def retention_could_cut_window(
    *,
    log_start: int,
    first_offset_in_window: int,
    now_ms: int,
    from_ms: int,
    retention_ms: int | None,
) -> bool:
    """Whether time retention may have deleted records inside the window.

    Only possible when the partition has been trimmed (``log_start > 0``) and
    the window's first record is the oldest one kept. Time retention deletes
    only records older than ``now - retention.ms``, so when that instant falls
    before the window starts, nothing in the window was deleted. That clears
    a quiet partition whose old segment aged out. An unknown retention counts
    as a possible cut; ``-1`` (infinite) never cuts. Deletion by
    ``retention.bytes`` is not detected here; the result records both settings
    so a reader can judge it.
    """
    if log_start <= 0 or first_offset_in_window != log_start:
        return False
    if retention_ms is None:
        return True
    if retention_ms < 0:
        return False
    return now_ms - retention_ms > from_ms


def _indeterminate(reasons: list[str]) -> JoinResult:
    return JoinResult(verdict=VERDICT_INDETERMINATE, indeterminate_reasons=reasons)


def _read_problems(commands: TopicRead, terminals: list[TopicRead]) -> list[str]:
    reasons: list[str] = []
    for read in [commands, *terminals]:
        if read.error is not None:
            reasons.append(f"topic {read.topic} unreadable: {read.error}")
        elif read.window_may_be_truncated:
            reasons.append(
                f"topic {read.topic}: the retained log starts inside the window, "
                "so records in it may have been deleted"
            )
    return reasons


def compute_join(
    *, window: Window, commands: TopicRead, terminals: list[TopicRead]
) -> JoinResult:
    """Join commands in the window to every terminal read. Pure; no I/O."""
    problems = _read_problems(commands, terminals)
    if problems:
        return _indeterminate(problems)

    in_window = [
        record
        for record in commands.records
        if window.commands_from_ms <= record.timestamp_ms < window.commands_to_ms
    ]
    command_counts = Counter(
        record.correlation_id for record in in_window if record.correlation_id
    )
    terminal_counts: defaultdict[str, int] = defaultdict(int)
    for read in terminals:
        for record in read.records:
            if (
                record.correlation_id
                and window.commands_from_ms
                <= record.timestamp_ms
                <= window.terminals_to_ms
            ):
                terminal_counts[record.correlation_id] += 1

    ids = sorted(command_counts)
    with_terminal = [cid for cid in ids if terminal_counts.get(cid, 0) > 0]
    duplicates = [cid for cid in ids if terminal_counts.get(cid, 0) > 1]
    return JoinResult(
        verdict=VERDICT_MEASURED if ids else VERDICT_NO_COMMANDS,
        command_records=len(in_window),
        command_records_without_correlation_id=sum(
            1 for record in in_window if not record.correlation_id
        ),
        correlation_ids=len(ids),
        correlation_ids_with_repeated_command=sum(
            1 for count in command_counts.values() if count > 1
        ),
        correlation_ids_with_terminal=len(with_terminal),
        terminal_share=(len(with_terminal) / len(ids)) if ids else None,
        dropped_ids=[cid for cid in ids if terminal_counts.get(cid, 0) == 0],
        duplicate_terminal_ids=duplicates,
        duplicate_terminals_with_single_command=sum(
            1 for cid in duplicates if command_counts[cid] == 1
        ),
    )


def _plant(
    window: Window, commands: TopicRead, terminals: list[TopicRead]
) -> tuple[TopicRead, list[TopicRead]]:
    """Add one dropped run and one double terminal, in memory only."""
    at = window.commands_from_ms + 1
    planted_commands = replace(
        commands,
        records=[
            *commands.records,
            Record(at, PLANTED_DROPPED_ID),
            Record(at, PLANTED_DUPLICATE_ID),
        ],
    )
    first, *rest = terminals
    planted_first = replace(
        first,
        records=[
            *first.records,
            Record(at + 1, PLANTED_DUPLICATE_ID),
            Record(at + 2, PLANTED_DUPLICATE_ID),
        ],
    )
    return planted_commands, [planted_first, *rest]


def _without_plants(result: JoinResult) -> JoinResult:
    plants = {PLANTED_DROPPED_ID, PLANTED_DUPLICATE_ID}
    assert result.dropped_ids is not None
    assert result.duplicate_terminal_ids is not None
    assert result.correlation_ids is not None
    assert result.correlation_ids_with_terminal is not None
    assert result.command_records is not None
    assert result.duplicate_terminals_with_single_command is not None
    ids = result.correlation_ids - 2
    with_terminal = result.correlation_ids_with_terminal - 1
    return replace(
        result,
        verdict=VERDICT_MEASURED if ids else VERDICT_NO_COMMANDS,
        command_records=result.command_records - 2,
        correlation_ids=ids,
        correlation_ids_with_terminal=with_terminal,
        terminal_share=(with_terminal / ids) if ids else None,
        dropped_ids=[cid for cid in result.dropped_ids if cid not in plants],
        duplicate_terminal_ids=[
            cid for cid in result.duplicate_terminal_ids if cid not in plants
        ],
        duplicate_terminals_with_single_command=(
            result.duplicate_terminals_with_single_command - 1
        ),
        planted_dropped_detected=True,
        planted_duplicate_detected=True,
    )


def measure_with_planted_controls(
    *, window: Window, commands: TopicRead, terminals: list[TopicRead]
) -> JoinResult:
    """Run the join with the planted controls and trust it only if it finds them."""
    problems = _read_problems(commands, terminals)
    if problems:
        return _indeterminate(problems)
    if not terminals:
        return _indeterminate(["no terminal topic was read"])
    planted_commands, planted_terminals = _plant(window, commands, terminals)
    result = compute_join(
        window=window, commands=planted_commands, terminals=planted_terminals
    )
    if result.verdict == VERDICT_INDETERMINATE:
        return result
    dropped_found = PLANTED_DROPPED_ID in (result.dropped_ids or [])
    duplicate_found = PLANTED_DUPLICATE_ID in (result.duplicate_terminal_ids or [])
    if not (dropped_found and duplicate_found):
        failed = _indeterminate(
            [
                "planted control not reported: "
                f"dropped={dropped_found} duplicate={duplicate_found}; "
                "the join cannot be trusted on this run"
            ]
        )
        failed.planted_dropped_detected = dropped_found
        failed.planted_duplicate_detected = duplicate_found
        return failed
    return _without_plants(result)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).isoformat().replace("+00:00", "Z")


def to_payload(
    *,
    result: JoinResult,
    window: Window,
    topics: DeclaredTopics,
    lane: str,
    reads: list[TopicRead] | None = None,
) -> dict[str, object]:
    def listed(ids: list[str] | None) -> list[str] | None:
        return None if ids is None else ids[:MAX_LISTED_IDS]

    return {
        "ticket": TICKET,
        "lane": lane,
        "verdict": result.verdict,
        "indeterminate_reasons": result.indeterminate_reasons,
        "window": {
            "commands_from": _iso(window.commands_from_ms),
            "commands_to": _iso(window.commands_to_ms),
            "terminals_to": _iso(window.terminals_to_ms),
            "grace_seconds": window.grace_ms // 1000,
        },
        "topics": {"command": topics.command, "terminals": list(topics.terminals)},
        "retention": {
            read.topic: {
                "retention_ms": read.retention_ms,
                "retention_bytes": read.retention_bytes,
            }
            for read in reads or []
        },
        "commands": {
            "records": result.command_records,
            "records_without_correlation_id": (
                result.command_records_without_correlation_id
            ),
            "correlation_ids": result.correlation_ids,
            "correlation_ids_with_repeated_command": (
                result.correlation_ids_with_repeated_command
            ),
        },
        "correlation_ids_with_terminal": result.correlation_ids_with_terminal,
        "terminal_share": result.terminal_share,
        "dropped": {
            "count": None if result.dropped_ids is None else len(result.dropped_ids),
            "ids": listed(result.dropped_ids),
        },
        "duplicate_terminals": {
            "count": (
                None
                if result.duplicate_terminal_ids is None
                else len(result.duplicate_terminal_ids)
            ),
            "with_single_command": result.duplicate_terminals_with_single_command,
            "ids": listed(result.duplicate_terminal_ids),
        },
        "planted_controls": {
            "dropped_detected": result.planted_dropped_detected,
            "duplicate_detected": result.planted_duplicate_detected,
        },
    }


def render_summary(payload: dict[str, object]) -> str:
    share = payload["terminal_share"]
    dropped = payload["dropped"]
    duplicates = payload["duplicate_terminals"]
    commands = payload["commands"]
    window = payload["window"]
    assert isinstance(dropped, dict)
    assert isinstance(duplicates, dict)
    assert isinstance(commands, dict)
    assert isinstance(window, dict)
    lines = [
        f"## Delegation terminal join ({TICKET}, read-only)",
        "",
        f"- lane: `{payload['lane']}`",
        f"- verdict: **{payload['verdict']}**",
        f"- commands window: {window['commands_from']} to {window['commands_to']} "
        f"(terminals read to {window['terminals_to']})",
        f"- correlation ids: {commands['correlation_ids']}",
        "- share with a terminal: "
        + ("n/a" if not isinstance(share, float) else f"{share:.4f}"),
        f"- dropped (no terminal): {dropped['count']}",
        f"- more than one terminal: {duplicates['count']} "
        f"(single command: {duplicates['with_single_command']})",
        f"- planted controls: {payload['planted_controls']}",
    ]
    reasons = payload["indeterminate_reasons"]
    if isinstance(reasons, list) and reasons:
        lines += ["", "**Indeterminate because:**", *[f"- {r}" for r in reasons]]
    return "\n".join(lines) + "\n"


Reader = Callable[[str, int, int], Awaitable[TopicRead]]


async def _gather(
    reader: Reader, topics: DeclaredTopics, window: Window
) -> tuple[TopicRead, list[TopicRead]]:
    async def safe(topic: str, to_ms: int) -> TopicRead:
        try:
            return await asyncio.wait_for(
                reader(topic, window.commands_from_ms, to_ms),
                timeout=READ_DEADLINE_SECONDS,
            )
        except Exception as exc:  # any read failure is a named indeterminate
            return TopicRead(
                topic=topic, records=[], error=f"{type(exc).__name__}: {exc}"
            )

    commands = await safe(topics.command, window.terminals_to_ms)
    terminals = [
        await safe(topic, window.terminals_to_ms) for topic in topics.terminals
    ]
    return commands, terminals


def run(
    *,
    reader: Reader,
    contract_path: Path,
    now_ms: int,
    window_hours: int,
    lane: str,
    out_path: Path,
    summary_path: Path | None = None,
) -> int:
    topics = load_declared_topics(contract_path)
    window = window_for(
        now_ms=now_ms, window_hours=window_hours, grace_ms=topics.max_timeout_ms
    )
    commands, terminals = asyncio.run(_gather(reader, topics, window))
    result = measure_with_planted_controls(
        window=window, commands=commands, terminals=terminals
    )
    payload = to_payload(
        result=result,
        window=window,
        topics=topics,
        lane=lane,
        reads=[commands, *terminals],
    )
    out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    summary = render_summary(payload)
    sys.stdout.write(summary)
    if summary_path is not None:
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    return EXIT_INDETERMINATE if result.verdict == VERDICT_INDETERMINATE else EXIT_OK


def kafka_reader(
    *, bootstrap_servers: str, security_protocol: str, sasl_mechanism: str
) -> Reader:
    """A group-less aiokafka reader of one topic between two timestamps."""

    auth = {
        "bootstrap_servers": bootstrap_servers,
        "security_protocol": security_protocol,
        "sasl_mechanism": sasl_mechanism or "PLAIN",
        "sasl_plain_username": os.environ.get("KAFKA_SASL_USERNAME") or None,
        "sasl_plain_password": os.environ.get("KAFKA_SASL_PASSWORD") or None,
    }

    async def _topic_retention(topic: str) -> tuple[int | None, int | None]:
        """retention.ms and retention.bytes, or None where they cannot be read."""
        from aiokafka.admin import AIOKafkaAdminClient
        from aiokafka.admin.config_resource import (
            ConfigResource,
            ConfigResourceType,
        )

        admin = AIOKafkaAdminClient(
            bootstrap_servers=bootstrap_servers,
            security_protocol=security_protocol,
            sasl_mechanism=auth["sasl_mechanism"],
            sasl_plain_username=auth["sasl_plain_username"],
            sasl_plain_password=auth["sasl_plain_password"],
        )
        values: dict[str, int] = {}
        try:
            await admin.start()
            responses = await admin.describe_configs(
                [
                    ConfigResource(
                        ConfigResourceType.TOPIC,
                        topic,
                        configs={"retention.ms": None, "retention.bytes": None},
                    )
                ]
            )
            for response in responses:
                for resource in response.resources:
                    for entry in resource[4]:
                        with contextlib.suppress(TypeError, ValueError):
                            values[str(entry[0])] = int(entry[1])
        except Exception:  # unknown retention is treated as a possible cut
            return None, None
        finally:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await admin.close()
        return values.get("retention.ms"), values.get("retention.bytes")

    async def read(topic: str, from_ms: int, to_ms: int) -> TopicRead:
        from aiokafka import AIOKafkaConsumer

        # The topic goes to the CONSTRUCTOR, not to assign() afterwards:
        # partitions_for_topic() reads cached metadata, which holds only
        # subscribed topics, so a bare consumer plus assign() reports a live
        # topic as absent (measured here on 2026-09-24, and first found by
        # omnibase_infra's chain canary). With no group_id there is no
        # coordinator: the partitions are assigned locally, nothing joins a
        # group and nothing is committed.
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=None,
            enable_auto_commit=False,
            auto_offset_reset="none",
            security_protocol=security_protocol,
            sasl_mechanism=sasl_mechanism or "PLAIN",
            sasl_plain_username=os.environ.get("KAFKA_SASL_USERNAME") or None,
            sasl_plain_password=os.environ.get("KAFKA_SASL_PASSWORD") or None,
        )
        try:
            await consumer.start()
            tps = sorted(consumer.assignment(), key=lambda tp: tp.partition)
            if not tps:
                return TopicRead(
                    topic=topic,
                    records=[],
                    error="no partitions (absent, or not readable by this principal)",
                )
            starts = await consumer.beginning_offsets(tps)
            ends = await consumer.end_offsets(tps)
            by_time = await consumer.offsets_for_times(dict.fromkeys(tps, from_ms))
            retention_ms, retention_bytes = await _topic_retention(topic)
            truncated = False
            records: list[Record] = []
            pending = []
            for tp in tps:
                found = by_time.get(tp)
                if found is None:
                    # Nothing at or after from_ms here; park it at its end so
                    # the fetcher never needs an offset reset for it.
                    consumer.seek(tp, ends[tp])
                    continue
                if retention_could_cut_window(
                    log_start=starts[tp],
                    first_offset_in_window=found.offset,
                    now_ms=to_ms,
                    from_ms=from_ms,
                    retention_ms=retention_ms,
                ):
                    truncated = True
                if found.offset < ends[tp]:
                    consumer.seek(tp, found.offset)
                    pending.append(tp)
            for tp in pending:
                while await consumer.position(tp) < ends[tp]:
                    batch = await consumer.getmany(tp, timeout_ms=5000)
                    for message in batch.get(tp, []):
                        if message.offset >= ends[tp]:
                            break
                        if from_ms <= message.timestamp <= to_ms:
                            records.append(
                                Record(
                                    message.timestamp,
                                    extract_correlation_id(message.value),
                                )
                            )
            return TopicRead(
                topic=topic,
                records=records,
                window_may_be_truncated=truncated,
                retention_ms=retention_ms,
                retention_bytes=retention_bytes,
            )
        finally:
            # A stop that fails must not replace the read's own result or error.
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await consumer.stop()

    return read


def _resolution_name(mode: str) -> str:
    """Name a lane resolution by a literal chosen by comparison.

    The resolved value is never formatted into a message: one resolution
    constant is named for the injected secret, and CodeQL's clear-text-logging
    rule follows that name into any message that interpolates the value
    (alert 1189 on omnimarket#2860). The literals equal the constants' values.
    """
    if mode == "no-lane":
        return "'no-lane'"
    if mode == "unknown-lane":
        return "'unknown-lane'"
    if mode == "inmemory":
        return "'inmemory'"
    if mode == "from-secret":
        return "'from-secret'"
    return "an unrecognised resolution"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--lane", default="dev")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = parser.parse_args(argv)

    from ci_bus_lanes import (
        MODE_CONCRETE,
        LaneSecurityError,
        load_lane_overlay,
        resolve_lane_broker,
        resolve_lane_security,
    )

    overlay = load_lane_overlay()
    mode, broker = resolve_lane_broker(overlay, args.lane)
    if mode != MODE_CONCRETE:
        sys.stderr.write(
            f"ERROR: lane {args.lane!r} resolves to {_resolution_name(mode)}, not a "
            "concrete broker in config/ci_bus_lanes.yaml; refusing to read an "
            "unspecified broker\n"
        )
        return EXIT_INVOCATION
    try:
        protocol, mechanism = resolve_lane_security(overlay, args.lane)
    except LaneSecurityError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return EXIT_INVOCATION

    summary_env = os.environ.get("GITHUB_STEP_SUMMARY", "")
    return run(
        reader=kafka_reader(
            bootstrap_servers=broker,
            security_protocol=protocol,
            sasl_mechanism=mechanism,
        ),
        contract_path=args.contract,
        now_ms=int(time.time() * 1000),
        window_hours=args.window_hours,
        lane=args.lane,
        out_path=args.out,
        summary_path=Path(summary_env) if summary_env else None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
