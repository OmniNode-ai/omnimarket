# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open one test pull request, wait for the landing tick to merge it, record how long it took.

The daily synthetic landing canary of piece 9 (landing and merge queue) of the base-first plan. While
fewer than 10 eligible pull requests wait, the landing goal cannot be read from throughput, so this
run stands in for it: the test pull request lands inside the overlay's bar or the reading is NOT
WORKING. WORKING needs the merge inside the bar. NOT_WORKING names which way it failed: merged late,
closed unmerged, or still open when the bar ran out (a stalled landing tick). A pull request that
cannot be opened, or whose state cannot be read at the end, gives UNKNOWN with a typed reason. The
run changes nothing but the one test pull request in the test repository and is never a gate.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_landing_canary_effect.models.model_landing_canary import (
    EnumCanaryEnding,
    ModelCanaryOpened,
    ModelCanaryOpenRequest,
    ModelCanaryState,
    ModelLandingCanaryCommand,
    ModelLandingCanaryResult,
)
from omnimarket.nodes.node_landing_canary_effect.overlay import (
    ModelLandingCanaryOverlay,
)
from omnimarket.nodes.node_landing_canary_effect.protocols import (
    CanaryRepoError,
    ProtocolCanaryRepo,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_ROW_TYPE = "STATUS"
_ROW_SOURCE_CELL = "src=landing-canary"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


class HandlerLandingCanary:
    """Run the landing canary once."""

    def __init__(
        self,
        *,
        overlay: ModelLandingCanaryOverlay,
        host: str,
        repo: ProtocolCanaryRepo,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
        sleep: Callable[[float], None],
    ) -> None:
        self._overlay = overlay
        self._host = host
        self._repo = repo
        self._appender = appender
        self._clock = clock
        self._sleep = sleep

    def handle(self, command: ModelLandingCanaryCommand) -> ModelLandingCanaryResult:
        if command.write and self._appender is None:
            return ModelLandingCanaryResult(
                correlation_id=command.correlation_id,
                status="failed",
                host=self._host,
                failure="no-appender-for-a-writing-run",
            )
        started = self._clock().astimezone(UTC)
        result = self._run(command.correlation_id, started)
        return self._publish(result, started, command.write)

    def _result(
        self,
        correlation_id: str,
        *,
        verdict: EnumReadingVerdict,
        outcome: EnumReadingOutcome,
        ending: EnumCanaryEnding,
        reasons: tuple[str, ...],
        opened: ModelCanaryOpened | None = None,
        elapsed: int | None = None,
    ) -> ModelLandingCanaryResult:
        target = self._overlay.target
        return ModelLandingCanaryResult(
            correlation_id=correlation_id,
            status="completed",
            host=self._host,
            verdict=verdict,
            outcome=outcome,
            ending=ending,
            reasons=reasons,
            repo=target.repo,
            pr_number=opened.number if opened else None,
            head_sha=opened.head_sha if opened else "",
            elapsed_seconds=elapsed,
            bound_seconds=target.landing_bound_seconds,
        )

    def _run(self, correlation_id: str, started: datetime) -> ModelLandingCanaryResult:
        target = self._overlay.target
        nonce = _sha(f"{correlation_id}|{stamp_text(started)}")[:12]
        request = ModelCanaryOpenRequest(
            repo=target.repo,
            base=target.base,
            branch=f"{target.branch_prefix}{nonce}",
            nonce=nonce,
            title=f"{target.title_prefix} {nonce}",
        )
        try:
            opened = self._repo.open(request)
        except CanaryRepoError as exc:
            return self._result(
                correlation_id,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                ending=EnumCanaryEnding.OPEN_FAILED,
                reasons=(reason_token(f"canary-open-failed-{exc}"),),
            )
        opened_at = self._clock().astimezone(UTC)
        state, unreadable = self._wait(opened.number, opened_at)
        now = self._clock().astimezone(UTC)
        if state is None:
            return self._result(
                correlation_id,
                verdict=EnumReadingVerdict.UNKNOWN,
                outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
                ending=EnumCanaryEnding.STATE_UNREADABLE,
                reasons=(reason_token(f"state-unreadable-{unreadable}"),),
                opened=opened,
                elapsed=int((now - opened_at).total_seconds()),
            )
        bound = target.landing_bound_seconds
        if state.merged:
            merged_at = (state.merged_at or now).astimezone(UTC)
            elapsed = max(0, int((merged_at - opened_at).total_seconds()))
            if elapsed <= bound:
                return self._result(
                    correlation_id,
                    verdict=EnumReadingVerdict.WORKING,
                    outcome=EnumReadingOutcome.READ,
                    ending=EnumCanaryEnding.MERGED,
                    reasons=(f"landed-in-{elapsed}s-inside-{bound}s",),
                    opened=opened,
                    elapsed=elapsed,
                )
            return self._result(
                correlation_id,
                verdict=EnumReadingVerdict.NOT_WORKING,
                outcome=EnumReadingOutcome.READ,
                ending=EnumCanaryEnding.MERGED_LATE,
                reasons=(f"landed-in-{elapsed}s-over-{bound}s",),
                opened=opened,
                elapsed=elapsed,
            )
        elapsed = int((now - opened_at).total_seconds())
        if state.closed:
            return self._result(
                correlation_id,
                verdict=EnumReadingVerdict.NOT_WORKING,
                outcome=EnumReadingOutcome.READ,
                ending=EnumCanaryEnding.CLOSED_UNMERGED,
                reasons=("closed-unmerged",),
                opened=opened,
                elapsed=elapsed,
            )
        return self._result(
            correlation_id,
            verdict=EnumReadingVerdict.NOT_WORKING,
            outcome=EnumReadingOutcome.READ,
            ending=EnumCanaryEnding.STALLED,
            reasons=(f"unmerged-after-{elapsed}s-bound-{bound}s",),
            opened=opened,
            elapsed=elapsed,
        )

    def _wait(
        self, number: int, opened_at: datetime
    ) -> tuple[ModelCanaryState | None, str]:
        """Read the pull request each poll until it merges, closes, or the bar has run out.

        The last read counts. A read that fails is tried again at the next poll; if the final read
        failed, the state is unknown and the reason of that failure is returned.
        """
        target = self._overlay.target
        deadline = opened_at.timestamp() + target.landing_bound_seconds
        last: ModelCanaryState | None = None
        failure = "no-read"
        while True:
            try:
                last = self._repo.state(number)
                failure = ""
            except CanaryRepoError as exc:
                last = None
                failure = str(exc)
            if last is not None and (last.merged or last.closed):
                return last, ""
            if self._clock().timestamp() >= deadline:
                return last, failure
            self._sleep(float(target.poll_seconds))

    def _publish(
        self, result: ModelLandingCanaryResult, now: datetime, write: bool
    ) -> ModelLandingCanaryResult:
        overlay_hash = self._overlay.overlay_hash()
        key = _sha(
            "|".join((self._host, result.correlation_id, stamp_text(now), overlay_hash))
        )[:32]
        cells_by_name = {
            "repo": result.repo or "none",
            "pr": str(result.pr_number) if result.pr_number else "none",
            "head": result.head_sha or "none",
            "ending": result.ending.value if result.ending else "none",
            "elapsed_s": str(result.elapsed_seconds)
            if result.elapsed_seconds is not None
            else "none",
            "bound_s": str(result.bound_seconds)
            if result.bound_seconds is not None
            else "none",
        }
        content = _sha(
            json.dumps(
                [
                    result.verdict.value,
                    result.outcome.value,
                    list(result.reasons),
                    cells_by_name,
                ],
                sort_keys=True,
            )
        )[:32]
        result = result.model_copy(update={"row_key": key})
        if not write:
            return result
        assert self._appender is not None
        publish = self._overlay.publish
        cells = [
            f"lane={publish.lane}",
            _ROW_SOURCE_CELL,
            f"actor={publish.actor}",
            "model=none",
            f"host={self._host}",
            f"piece={publish.piece}",
            f"tag={'drill' if publish.drill else 'operational'}",
            f"backend={self._overlay.target.backend}",
            f"verdict={result.verdict.value}",
            f"outcome={result.outcome.value}",
            f"reason={','.join(result.reasons) or 'none'}",
            *(f"{name}={value}" for name, value in cells_by_name.items()),
            f"probed_at={stamp_text(now)}",
            f"published_at={stamp_text(self._clock())}",
            f"max_age={publish.publish_max_age_seconds}",
            f"key={key}",
            f"content={content}",
            f"overlay_hash={overlay_hash[:12]}",
            _clean(f"injected fault: {self._overlay.injected_fault}"),
        ]
        row = f"{stamp_text(now)} | {_ROW_TYPE} | " + " | ".join(cells)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{key}|{content}"))
        appended = self._appender.append(row + "\n", request_id)
        if not appended.ok:
            return result.model_copy(
                update={"ledger": f"append-failed:{appended.status}", "row": row}
            )
        return result.model_copy(
            update={
                "ledger": "written"
                if appended.status == "accepted"
                else "already-written",
                "row": row,
            }
        )


def events_of(
    result: ModelLandingCanaryResult, topics: dict[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The events a run produces, in order, all under its correlation id."""
    if result.status == "failed":
        return [
            (
                topics["landing_canary_run_failed"],
                {"correlation_id": result.correlation_id, "failure": result.failure},
            )
        ]
    body: dict[str, object] = {
        "correlation_id": result.correlation_id,
        "host": result.host,
        "verdict": result.verdict.value,
        "outcome": result.outcome.value,
        "ending": result.ending.value if result.ending else None,
        "reasons": list(result.reasons),
        "pr_number": result.pr_number,
        "elapsed_seconds": result.elapsed_seconds,
        "bound_seconds": result.bound_seconds,
        "row_key": result.row_key,
        "ledger": result.ledger,
    }
    return [
        (
            topics["landing_canary_failed"]
            if result.failed
            else topics["landing_canary_written"],
            body,
        ),
        (
            topics["landing_canary_run_completed"],
            {
                "correlation_id": result.correlation_id,
                "host": result.host,
                "verdict": result.verdict.value,
                "failed": int(result.failed),
            },
        ),
    ]


def answer_command(
    payload: bytes, handler: HandlerLandingCanary, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the events it produces out, each as (topic, JSON bytes).

    A payload that is not a command is answered with a typed failure event, never dropped.
    """
    try:
        command = ModelLandingCanaryCommand.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = {"correlation_id": "unknown", "failure": f"bad-payload:{fields}"}
        return [(topics["landing_canary_run_failed"], json.dumps(failure).encode())]
    return [
        (topic, json.dumps(body, sort_keys=True).encode())
        for topic, body in events_of(handler.handle(command), topics)
    ]


__all__ = ["HandlerLandingCanary", "answer_command", "events_of", "load_topics"]
