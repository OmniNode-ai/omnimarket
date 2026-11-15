# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Standing per-key credit check for the review gateway's keys (OMN-24562).

A review key that runs out of credit makes a voter answer 503 until someone notices. The gateway
skips such a key on its own, but nothing told the operator that a key had flipped. This module
keeps the last state of each key in a state file and, when a key's state differs from the last
run, appends one ledger STATUS row carrying ``alarm=review-key-credit``. The first run records
the baseline and writes no row. Only a key's display name is ever written, never its credential
reference, its value or a provider message.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

PROBE_PAYLOAD: dict[str, object] = {
    "messages": [{"role": "user", "content": "Reply with the single word OK."}],
    "max_tokens": 1,
}
_STATE_FILE = "key-states.json"
LEDGER_BIN_ENV = "ONEX_REVIEW_KEY_PROBE_LEDGER_BIN"


def _safe(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def read_states(state_dir: Path) -> dict[str, str]:
    """The last run's key states, empty when there was no readable run."""
    try:
        raw = json.loads((state_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def write_states(state_dir: Path, states: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _STATE_FILE
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(states, sort_keys=True), encoding="utf-8")
    temp.replace(target)


def flips(
    previous: dict[str, str], current: dict[str, str]
) -> list[tuple[str, str, str]]:
    """(key, from, to) for each key whose state changed since a recorded run.

    A key with no recorded state is a baseline, not a flip, and an ``unreachable`` reading
    (the call failed for a reason that says nothing about the key) never flips a key.
    """
    return [
        (name, previous[name], state)
        for name, state in sorted(current.items())
        if name in previous
        and state != "unreachable"
        and previous[name] != "unreachable"
        and previous[name] != state
    ]


def render_alarm_row(
    stamp: str, host: str, model: str, name: str, before: str, after: str
) -> str:
    """One rolling-ledger STATUS row, no data-supplied pipe delimiters."""
    return (
        f"{_safe(stamp)} | STATUS | lane=review-key-credit-probe | ticket=OMN-24562 | "
        f"actor=launchd:review-key-credit-probe | alarm=review-key-credit | "
        f"host={_safe(host)} | model={_safe(model)} | key={_safe(name)} | "
        f"from={_safe(before)} | to={_safe(after)} | review key {_safe(name)} on host "
        f"{_safe(host)} went from {_safe(before)} to {_safe(after)}"
    )


def append_ledger(ledger: Path, row: str) -> tuple[bool, str]:
    """Append through the ledger command, never through a direct file write."""
    argv = shlex.split(os.environ.get(LEDGER_BIN_ENV, "onex-ledger"))
    try:
        done = subprocess.run(
            [*argv, str(ledger), "--append", row],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"ledger append did not run: {type(exc).__name__}"
    return done.returncode == 0, f"ledger append exit {done.returncode}"


def record(
    states: Sequence[tuple[str, str]],
    *,
    state_dir: Path,
    ledger: Path,
    host: str,
    model: str,
    appender: Callable[[Path, str], tuple[bool, str]] = append_ledger,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[tuple[str, str, str]]:
    """Compare this run with the last, write one row per flip, then keep the new states.

    The state file is replaced only when every row was written, so a ledger that refused is
    retried on the next run instead of the flip being lost.
    """
    current = dict(states)
    previous = read_states(state_dir)
    changed = flips(previous, current)
    stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
    for name, before, after in changed:
        written, detail = appender(
            ledger, render_alarm_row(stamp, host, model, name, before, after)
        )
        if not written:
            raise RuntimeError(f"review key flip not recorded: {detail}")
    kept = {
        name: (state if state != "unreachable" else previous.get(name, state))
        for name, state in current.items()
    }
    write_states(state_dir, kept)
    return changed
