# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The readers behind the beta board (OMN-23315): a probe workflow's runs through the source-control node, ledger rows, and the saved reads."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardRuntime,
    ModelBetaLedgerReading,
    ModelBetaProbe,
    ModelReadingPoint,
    ModelSourceRead,
    ModelSourceReads,
    ReadingVerdict,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

CommandRunner = Callable[[Sequence[str], int], tuple[int, str, str]]
CopyRunner = Callable[[Sequence[str], bytes, int], tuple[int, str]]

_RUN_PASS = frozenset({"success"})
_RUN_FAIL = frozenset({"failure", "timed_out", "startup_failure"})


def run_command(argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout} s"
    except OSError as exc:
        return 127, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def run_copy(argv: Sequence[str], payload: bytes, timeout: int) -> tuple[int, str]:
    try:
        done = subprocess.run(
            list(argv), input=payload, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout} s"
    except OSError as exc:
        return 127, str(exc)
    tail = " | ".join(done.stderr.decode("utf-8", "replace").strip().splitlines()[-2:])
    return done.returncode, tail


def parse_moment(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def read_probe(
    probe: ModelBetaProbe,
    rt: ModelBetaBoardRuntime,
    onex: Path,
    run: CommandRunner,
    now: datetime,
) -> ModelSourceRead:
    """One probe's finished runs, newest first, through the source-control node; a run that is neither a success nor a failure is not a reading."""
    argv = [
        str(onex),
        "scm",
        "run-list",
        probe.repository,
        "--workflow",
        probe.workflow,
        "--limit",
        str(rt.probe_run_limit),
    ]
    if len(probe.events) == 1:
        argv += ["--event", probe.events[0]]
    code, out, err = run(argv, rt.probe_read_timeout_seconds)
    answer = _json_line(out)

    def unread(reason: str) -> ModelSourceRead:
        return ModelSourceRead(
            source_id=probe.id,
            kind="workflow_run",
            read="unread",
            read_at=now,
            reason=reason[:240],
        )

    if answer is None:
        tail = (err or out).strip().splitlines()[-1:] or ["no answer"]
        return unread(f"exit {code}: {tail[0]}")
    result = answer.get("result")
    if not isinstance(result, dict):
        return unread("the answer has no result")
    if answer.get("outcome") != "completed":
        return unread(
            str(result.get("failure_reason") or answer.get("outcome") or "failed")
        )
    runs = result.get("runs")
    if not isinstance(runs, list):
        return unread("the answer lists no runs")
    points: list[ModelReadingPoint] = []
    for item in runs:
        if not isinstance(item, dict) or item.get("status") != "completed":
            continue
        if probe.events and str(item.get("event") or "") not in probe.events:
            continue
        conclusion = str(item.get("conclusion") or "")
        verdict: ReadingVerdict
        if conclusion in _RUN_PASS:
            verdict = "pass"
        elif conclusion in _RUN_FAIL:
            verdict = "fail"
        else:
            continue
        moment = parse_moment(
            str(item.get("updated_at") or item.get("created_at") or "")
        )
        if moment is None:
            continue
        points.append(
            ModelReadingPoint(
                at=moment, verdict=verdict, ref=str(item.get("html_url") or "")
            )
        )
    return ModelSourceRead(
        source_id=probe.id,
        kind="workflow_run",
        read="read",
        read_at=now,
        reason="" if points else "no finished run of the workflow",
        points=tuple(points),
    )


def read_ledger_readings(
    source: ProtocolLedgerRowSource,
    readings: Sequence[ModelBetaLedgerReading],
    rt: ModelBetaBoardRuntime,
    now: datetime,
) -> dict[str, ModelSourceRead]:
    """Each ledger reading's dated verdicts from the rows the overlay's ledger route gives; a route that fails leaves that reading unread."""
    out: dict[str, ModelSourceRead] = {}
    for reading in readings:
        answer = source.read(
            ModelLedgerQuery(
                since=now - timedelta(seconds=reading.history_seconds),
                until=now,
                row_type=reading.row_type,
            )
        )
        if answer.error is not None:
            out[reading.id] = ModelSourceRead(
                source_id=reading.id,
                kind="ledger_row",
                read="unread",
                read_at=now,
                reason=f"the ledger cannot be read: {answer.error}"[:240],
            )
            continue
        points: list[ModelReadingPoint] = []
        for row in answer.rows:
            if row.row_type != reading.row_type:
                continue
            if any(row.cells.get(k) != v for k, v in reading.cells.items()):
                continue
            word = row.cells.get(reading.verdict_cell)
            if word is None:
                continue
            verdict: ReadingVerdict
            if word in reading.pass_values:
                verdict = "pass"
            elif word in reading.fail_values:
                verdict = "fail"
            else:
                verdict = "unknown"
            moment = (
                parse_moment(row.cells.get(reading.time_cell, ""))
                if reading.time_cell
                else None
            ) or row.stamp
            points.append(
                ModelReadingPoint(
                    at=moment,
                    verdict=verdict,
                    ref=f"ledger {row.stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}",
                )
            )
        points.sort(key=lambda p: p.at, reverse=True)
        out[reading.id] = ModelSourceRead(
            source_id=reading.id,
            kind="ledger_row",
            read="read",
            read_at=now,
            reason="" if points else "no ledger row matches this reading",
            points=tuple(points[: rt.ledger_row_cap]),
        )
    return out


def load_previous_reads(path: Path) -> dict[str, ModelSourceRead]:
    """The probe reads the last run kept, by source id; nothing when there is no usable file."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return {r.source_id: r for r in ModelSourceReads.model_validate(doc).reads}
    except (OSError, ValueError):
        return {}


def refresh_probes(
    rt: ModelBetaBoardRuntime,
    home: Path,
    now: datetime,
    run: CommandRunner,
    clock: Callable[[], datetime],
) -> ModelSourceReads:
    """Refresh the probe reads one at a time, the least recently read first, inside the stage deadline.

    A probe read within ``probe_refresh_seconds`` is kept as it is; one the deadline left no time for keeps its previous read,
    which the board then draws stale or unread by that probe's own bound.
    """
    kept = load_previous_reads(home / rt.reads_relpath)
    onex = home / rt.onex_relpath
    started = clock()
    result = dict(kept)
    ordered = sorted(
        rt.overlay.probes,
        key=lambda p: (
            kept[p.id].read_at if p.id in kept else datetime.min.replace(tzinfo=UTC)
        ),
    )
    for probe in ordered:
        previous = kept.get(probe.id)
        if (
            previous is not None
            and previous.read == "read"
            and (now - previous.read_at).total_seconds() < rt.probe_refresh_seconds
        ):
            continue
        if (clock() - started).total_seconds() >= rt.stage_deadline_seconds:
            break
        fresh = read_probe(probe, rt, onex, run, clock())
        # A failed read does not erase a good earlier one: the board ages it by the probe's own bound.
        if fresh.read == "unread" and previous is not None and previous.read == "read":
            continue
        result[probe.id] = fresh
    declared = {p.id for p in rt.overlay.probes}
    return ModelSourceReads(reads=tuple(r for k, r in result.items() if k in declared))


def write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target and rename into place, so a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def remote_write_command(remote_dir: str, name: str) -> str:
    """The remote shell command that stores stdin as ``remote_dir/name``: a temporary name in the same directory, renamed into place."""
    final = PurePosixPath(remote_dir) / name
    tmp = final.parent / f".{final.name}.tmp"
    q_dir, q_final, q_tmp = (shlex.quote(str(p)) for p in (final.parent, final, tmp))
    return (
        f"umask 022 && mkdir -p {q_dir} && "
        f"{{ cat > {q_tmp} && mv -f {q_tmp} {q_final}; }} || {{ rm -f {q_tmp}; exit 1; }}"
    )


def ssh_argv(connect_timeout: int, ssh_target: str, remote_command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={connect_timeout}",
        "--",
        ssh_target,
        remote_command,
    ]
