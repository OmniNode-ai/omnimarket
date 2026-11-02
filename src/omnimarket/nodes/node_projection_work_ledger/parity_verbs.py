# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-verb parity of the work ledger read verbs, file against database (OMN-21885, L1).

The daily parity receipt compares rows. Row parity alone does not license a reader flip: the
verbs answer from a fold of the rows, and the two sources can fold differently. ``verb_diff``
runs each read verb on both sources through the ledger readers (the ``onex-ledger`` command,
``--source file`` and ``--source db``) and counts, per verb, the answer lines the sources do not
share. This module reads nothing itself: it never opens the ledger file or the database.

A read that fails, or a database read that fell back to the file (``source=file-fallback``), is
``None`` for that verb and ``err`` in the receipt. It is never zero: a failed read is not a
difference of nothing.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

READ_VERBS: tuple[str, ...] = (
    "query",
    "holds",
    "inbox",
    "catchup",
    "open-rulings",
    "open-claims",
)
FALLBACK_MARK = "source=file-fallback"
INBOX_LANE = "work-ledger-parity"
QUERY_LIMIT = "1000"
# Seconds allowed to one verb read: open-claims over the whole ledger takes about 40 s.
VERB_TIMEOUT_S = 600.0
# Verbs that take --archives: the file answers from the live file and its archive splits.
_ARCHIVE_VERBS = frozenset({"query", "holds"})

#: ``run(args, source)`` -> the verb's stdout; ``source`` is ``file`` or ``db``.
VerbRun = Callable[[tuple[str, ...], str], str]
#: One invocation: the verb it counts toward and its argv (verb first, no --source).
VerbCall = tuple[str, tuple[str, ...]]

_RULING_TICKET = re.compile(r"\bticket=(OMN-\d+)\b")
# What differs between two reads of the same answer without the answer differing: a line number
# (the file counts lines, the database numbers by position), the clock, the freshness stamps and
# the detail file the verb wrote.
_VOLATILE = (
    (re.compile(r"(?m)(?:(?<=\s)|^)(?:\S+\.md:)?L\d+\b"), "L*"),
    (re.compile(r"\b(detail|age|now|newest|ledger-newest)=\S+"), r"\1=*"),
)


class VerbReadError(Exception):
    """A verb could not be read from one source; the difference is unknown, not zero."""


def ruling_tickets(rows: Sequence[str]) -> list[str]:
    """The tickets named by the RULING rows in ``rows``, sorted, once each. Pure."""
    found = {
        match.group(1)
        for raw in rows
        if " | RULING | " in raw
        for match in _RULING_TICKET.finditer(raw)
    }
    return sorted(found)


def verb_calls(day: str, tickets: Sequence[str]) -> tuple[VerbCall, ...]:
    """Every invocation behind ``verb_diff`` for UTC ``day``. Pure.

    ``open-rulings`` needs a ticket, so it runs once per ticket the day's RULING rows name."""
    start = f"{day}T00:00:00Z"
    calls: list[VerbCall] = [
        ("query", ("query", "--since", start, "--limit", QUERY_LIMIT)),
        ("holds", ("holds",)),
        ("inbox", ("inbox", INBOX_LANE)),
        ("catchup", ("catchup", "--since", start)),
    ]
    calls.extend(
        ("open-rulings", ("open-rulings", "--ticket", ticket)) for ticket in tickets
    )
    calls.append(("open-claims", ("open-claims",)))
    return tuple(calls)


def normalize_answer(text: str) -> Counter[str]:
    """The answer's lines with the volatile tokens masked, each counted as often as it occurs."""
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return Counter(line.rstrip() for line in text.splitlines() if line.strip())


def _surplus(ours: Counter[str], theirs: Counter[str]) -> int:
    return sum((ours - theirs).values())


def run_verb_diff(run: VerbRun, calls: Sequence[VerbCall]) -> dict[str, int | None]:
    """Per read verb, the answer lines one source has and the other lacks. ``None`` = unread."""
    diff: dict[str, int | None] = dict.fromkeys(READ_VERBS, 0)
    for verb, args in calls:
        if diff[verb] is None:
            continue
        try:
            file_answer = normalize_answer(run(args, "file"))
            db_answer = normalize_answer(run(args, "db"))
        except VerbReadError:
            diff[verb] = None
            continue
        diff[verb] = (
            (diff[verb] or 0)
            + _surplus(file_answer, db_answer)
            + _surplus(db_answer, file_answer)
        )
    return diff


def format_verb_diff(diff: Mapping[str, int | None] | None) -> str:
    """The receipt cell value: ``verb:N`` per verb, ``verb:err`` when unread, ``not-run``."""
    if diff is None:
        return "not-run"
    return ",".join(
        f"{verb}:{'err' if count is None else count}" for verb, count in diff.items()
    )


def subprocess_verb_run(
    command: Sequence[str],
    *,
    ledger: Path,
    detail_dir: Path,
    timeout: float,
) -> VerbRun:
    """A ``VerbRun`` that runs ``command <verb> ... --source <source> --ledger <ledger>``."""
    detail_dir.mkdir(parents=True, exist_ok=True)

    def run(args: tuple[str, ...], source: str) -> str:
        argv = [
            *command,
            *args,
            "--source",
            source,
            "--ledger",
            str(ledger),
            "--detail-dir",
            str(detail_dir),
        ]
        if args[0] in _ARCHIVE_VERBS:
            argv.append("--archives")
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VerbReadError(f"{args[0]} --source {source}: {exc!r}") from exc
        if done.returncode != 0 or FALLBACK_MARK in done.stdout + done.stderr:
            raise VerbReadError(
                f"{args[0]} --source {source}: exit {done.returncode}"
                + (
                    f" ({FALLBACK_MARK})"
                    if FALLBACK_MARK in done.stdout + done.stderr
                    else ""
                )
            )
        return done.stdout

    return run


def day_verb_diff(
    verb_command: str | None,
    day: str,
    file_rows: Sequence[str],
    ledger: Path,
    timeout: float = VERB_TIMEOUT_S,
) -> dict[str, int | None] | None:
    """``verb_diff`` for UTC ``day``; None (receipt: ``not-run``) when no command was given."""
    if not verb_command:
        return None
    with tempfile.TemporaryDirectory(prefix="parity-verbs-") as detail_dir:
        run = subprocess_verb_run(
            split_command(verb_command),
            ledger=ledger,
            detail_dir=Path(detail_dir),
            timeout=timeout,
        )
        day_rows = [raw for raw in file_rows if raw[:10] == day]
        return run_verb_diff(run, verb_calls(day, ruling_tickets(day_rows)))


def split_command(value: str) -> list[str]:
    """``--verb-command`` as an argv prefix; an empty command is an error, never a default."""
    argv = shlex.split(value)
    if not argv:
        raise ValueError("--verb-command names no program")
    return argv


__all__: list[str] = [
    "FALLBACK_MARK",
    "READ_VERBS",
    "VerbCall",
    "VerbReadError",
    "VerbRun",
    "day_verb_diff",
    "format_verb_diff",
    "normalize_answer",
    "ruling_tickets",
    "run_verb_diff",
    "split_command",
    "subprocess_verb_run",
    "verb_calls",
]
