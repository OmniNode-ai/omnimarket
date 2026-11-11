# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a new lane start passes to the pool's ``resolve`` when account steering is switched on (OMN-23636).

The lane start draws its engine login from the host's token pool. With the overlay's ``steering.enabled``
off (how it ships) this handler answers ``None`` and the draw is what it was. Switched on, it

* asks the usage verdict for the labels close to their limit, by running the command the overlay names
  (``steering.verdict_argv``), which prints the verdict node's result as JSON, and reading that result's
  ``drain_labels``;
* counts the lanes running under each label from the open CLAIM rows of the ledger that name their login
  (a ``login=<label>`` cell) and are no older than the overlay's bound.

A verdict that cannot be had (the command fails, times out or prints something else) steers nothing: the
start goes ahead with an empty drain list and says why. A missing verdict never blocks a lane; the lane cap,
which the pool holds the counts against, is what refuses one. Labels only pass through here, never a token.

The count is a reading, not a reservation: two starts that count at the same moment both see the same
number, so the cap can be passed by the number of starts that draw together, and a lane that outlives the
age bound without a closing row is no longer counted. The cap bounds a pile-up; it is not an exact limit.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.engine_login import (
    LABEL_PATTERN,
    ModelEngineLoginLaneCount,
    ModelEngineLoginSteer,
)
from omnimarket.nodes.node_claude_token_pool_effect.models.model_claude_token_rotation import (
    ModelRotationOverlay,
    ModelRotationSteering,
)

NODE_NAME = "node_claude_token_pool_effect"
_LABEL = re.compile(LABEL_PATTERN)
_ROW_TIME = "%Y-%m-%dT%H:%M:%SZ"

#: Runs the verdict command: the argv and its deadline in, the exit code and stdout back.
VerdictRunner = Callable[[Sequence[str], float], tuple[int, str]]


class ProtocolLedgerText(Protocol):
    """The ledger as the lane start reads it."""

    def read_text(self) -> str: ...


class ModelPoolDrainAnswer(BaseModel):
    """The one field this side reads from the usage verdict node's result.

    The name and the element type are the verdict node's ``ModelPoolUsageResult.drain_labels``; the
    seam test fails when either side moves. Every other field of the result is the verdict's own.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    drain_labels: tuple[str, ...]


def run_verdict_command(argv: Sequence[str], timeout: float) -> tuple[int, str]:
    """Run the overlay's verdict command; a command that cannot run or does not end answers a failure."""
    try:
        done = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as failed:
        return 127, type(failed).__name__
    return done.returncode, done.stdout


def count_open_lanes(
    ledger_text: str, *, now: datetime, max_age_seconds: int
) -> tuple[ModelEngineLoginLaneCount, ...]:
    """The lanes running under each label: open CLAIM rows that name a login and are within the bound.

    A lane is open while its latest CLAIM has no TERMINAL after it. A CLAIM with no ``login=`` cell, or
    ``login=none``, started on no pool label and counts under none. A CLAIM older than the bound is a
    lane that ended without its closing row, and is not counted.
    """
    open_logins: dict[str, tuple[str, str]] = {}
    for line in ledger_text.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3 or cells[1] not in ("CLAIM", "TERMINAL"):
            continue
        lane = next((c[5:] for c in cells[2:] if c.startswith("lane=")), "")
        if not lane:
            continue
        if cells[1] == "TERMINAL":
            open_logins.pop(lane, None)
            continue
        login = next((c[6:] for c in cells[2:] if c.startswith("login=")), "")
        open_logins[lane] = (cells[0], login)
    counts: Counter[str] = Counter()
    for stamp, login in open_logins.values():
        if not _LABEL.match(login) or login == "none":
            continue
        try:
            claimed = datetime.strptime(stamp, _ROW_TIME).replace(tzinfo=UTC)
        except ValueError:
            continue
        if (now - claimed).total_seconds() <= max_age_seconds:
            counts[login] += 1
    return tuple(
        ModelEngineLoginLaneCount(label=label, lanes=lanes)
        for label, lanes in sorted(counts.items())
    )


class HandlerPoolSteering:
    """Answer the steer of one lane start, or ``None`` when steering is off."""

    def __init__(
        self,
        *,
        steering: ModelRotationSteering | None = None,
        ledger: ProtocolLedgerText | None = None,
        run: VerdictRunner = run_verdict_command,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._steering = steering
        self._ledger = ledger
        self._run = run
        self._now = now

    def handle(self) -> ModelEngineLoginSteer | None:
        steering = self._steering
        if steering is None or not steering.enabled:
            return None
        # The runtime boot builds this handler with no arguments (the contract binds it to the node), so an
        # instance with no ledger is steering off, never a guess at a ledger.
        if self._ledger is None:
            return None
        # The overlay model refuses an enabled block without these; the checks keep the types honest.
        if (
            steering.verdict_timeout_seconds is None
            or steering.open_claim_max_age_seconds is None
        ):
            return None
        drain, detail = self._drain_labels(
            steering.verdict_argv, steering.verdict_timeout_seconds
        )
        try:
            ledger_text = self._ledger.read_text()
        except FileNotFoundError:
            ledger_text = ""  # a ledger nobody has written to holds no open CLAIM
        return ModelEngineLoginSteer(
            drain_labels=drain,
            lane_counts=count_open_lanes(
                ledger_text,
                now=self._now(),
                max_age_seconds=steering.open_claim_max_age_seconds,
            ),
            detail=detail,
        )

    def _drain_labels(
        self, argv: Sequence[str], timeout: float
    ) -> tuple[tuple[str, ...], str]:
        code, out = self._run(argv, timeout)
        if code != 0:
            return (), f"verdict unavailable: the verdict command ended {code}"
        try:
            answer = ModelPoolDrainAnswer.model_validate(json.loads(out))
        except (ValueError, ValidationError) as bad:
            return (), f"verdict unavailable: {type(bad).__name__} reading its result"
        strangers = [label for label in answer.drain_labels if not _LABEL.match(label)]
        if strangers:
            return (
                (),
                f"verdict unavailable: {len(strangers)} drain label(s) are not pool labels",
            )
        return answer.drain_labels, ""


def lane_start_steer(
    ledger: ProtocolLedgerText,
) -> Callable[[], ModelEngineLoginSteer | None]:
    """The steer the lane start asks at its login draw, read from the deployed overlay at each ask.

    No pool overlay under the roots, or one that does not validate, is steering off: the draw is what
    it was before this block existed. The overlays repository's own check loads the overlay through the
    model, so a malformed one is refused there and never here in the middle of a lane start.
    """

    def steer() -> ModelEngineLoginSteer | None:
        try:
            if find_node_overlay(NODE_NAME) is None:
                return None
            overlay = load_structured_overlay(NODE_NAME, ModelRotationOverlay)
        except (NodeOverlayError, ValidationError, OSError):
            return None
        return HandlerPoolSteering(steering=overlay.steering, ledger=ledger).handle()

    return steer


__all__ = [
    "HandlerPoolSteering",
    "ModelPoolDrainAnswer",
    "ProtocolLedgerText",
    "VerdictRunner",
    "count_open_lanes",
    "lane_start_steer",
    "run_verdict_command",
]
