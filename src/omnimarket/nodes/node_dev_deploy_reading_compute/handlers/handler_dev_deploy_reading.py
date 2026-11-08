# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevDeployReading: piece 11's reading at a read time (OMN-22958).

Two rules, both judged against the request's own time and thresholds:

* receipts: every runtime-affecting merge older than the deadline has a PASS lab pass receipt
  for its exact sha, on the required lane, finished no later than ``merged_at + deadline``. A merge
  inside its deadline is pending and fails nothing. A receipt for another sha is no evidence.
* probe: the newest functional probe of the running runtime is not future-dated, is inside its max
  age, passed, and observed the exact sha it was asked for.

A failure of a readable source reads NOT_WORKING. A source that could not be read reads UNKNOWN only
when nothing readable already fails.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelDevDeployReading,
    ModelDevDeployReadingRequest,
    ModelLabPassReceiptFact,
    ModelRuntimeProbeFact,
)

_SHORT = 12


def _short(sha: str) -> str:
    return sha[:_SHORT]


def judge_receipts(
    request: ModelDevDeployReadingRequest,
) -> tuple[list[str], list[str], int, int]:
    """The failures, the pending merges, the merges checked and the merges receipted."""
    deadline = timedelta(seconds=request.receipt_deadline_seconds)
    by_sha: dict[str, list[ModelLabPassReceiptFact]] = {}
    for receipt in request.receipts:
        if receipt.lane == request.required_lane:
            by_sha.setdefault(receipt.sha, []).append(receipt)
    failures: list[str] = []
    pending: list[str] = []
    checked = receipted = 0
    for merge in sorted(request.merges, key=lambda m: (m.merged_at, m.sha)):
        if not merge.runtime_affecting:
            continue
        checked += 1
        tag = _short(merge.sha)
        age = request.now - merge.merged_at
        if age < timedelta(0):
            failures.append(f"merge-after-read:{tag}")
            continue
        # A receipt that finishes after the read time is not evidence yet.
        own = [r for r in by_sha.get(merge.sha, []) if r.finished_at <= request.now]
        passes = sorted(r.finished_at for r in own if r.result == "PASS")
        if any(
            merge.merged_at <= finished <= merge.merged_at + deadline
            for finished in passes
        ):
            receipted += 1
            continue
        if age <= deadline:
            # An unusable receipt does not fail a merge that still has time to get a usable one.
            pending.append(f"receipt-pending:{tag}")
            continue
        if passes:
            finished = passes[0]
            if finished < merge.merged_at:
                failures.append(f"receipt-before-merge:{tag}")
            else:
                late = int((finished - merge.merged_at).total_seconds())
                failures.append(
                    f"receipt-late:{tag}:{late}s-over-{request.receipt_deadline_seconds}s"
                )
            continue
        failures.append(
            f"no-pass-receipt:{tag}:age-{int(age.total_seconds())}s-over-{request.receipt_deadline_seconds}s"
        )
        if any(r.result == "FAIL" for r in own):
            failures.append(f"receipt-fail:{tag}")
    return failures, pending, checked, receipted


def judge_probe(
    request: ModelDevDeployReadingRequest,
) -> tuple[list[str], int | None]:
    """The failures of the newest probe and its age, none when there is no probe to age."""
    if not request.probes:
        return ["probe-missing"], None
    newest: ModelRuntimeProbeFact = max(
        request.probes, key=lambda p: (p.probed_at, p.probe_id)
    )
    elapsed = request.now - newest.probed_at
    if elapsed < timedelta(0):
        return [f"probe-future-dated:{newest.probe_id}"], None
    # Eligibility keeps the timestamp's precision; the reported age is whole seconds.
    age = int(elapsed.total_seconds())
    failures: list[str] = []
    if elapsed > timedelta(seconds=request.probe_max_age_seconds):
        failures.append(
            f"probe-stale:{newest.probe_id}:age-{age}s-over-{request.probe_max_age_seconds}s"
        )
    if not newest.ok:
        failures.append(f"probe-failed:{newest.probe_id}")
    if newest.observed_sha is None:
        failures.append(f"probe-sha-unobserved:{newest.probe_id}")
    elif newest.observed_sha != newest.expected_sha:
        failures.append(
            f"probe-sha-mismatch:{newest.probe_id}:{_short(newest.expected_sha)}"
            f"-observed-{_short(newest.observed_sha)}"
        )
    return failures, age


class HandlerDevDeployReading:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(self, request: ModelDevDeployReadingRequest) -> ModelDevDeployReading:
        failures: list[str] = []
        unread: list[str] = []
        pending: list[str] = []
        checked = receipted = 0
        probe_age: int | None = None
        if request.receipts_unread_reason is not None:
            unread.append(f"receipts-unreadable:{request.receipts_unread_reason}")
        else:
            receipt_failures, pending, checked, receipted = judge_receipts(request)
            failures += receipt_failures
        if request.probes_unread_reason is not None:
            unread.append(f"probes-unreadable:{request.probes_unread_reason}")
        else:
            probe_failures, probe_age = judge_probe(request)
            failures += probe_failures
        if failures:
            verdict = EnumReadingVerdict.NOT_WORKING
        elif unread:
            verdict = EnumReadingVerdict.UNKNOWN
        else:
            verdict = EnumReadingVerdict.WORKING
        return ModelDevDeployReading(
            verdict=verdict,
            reasons=tuple(failures + unread),
            pending=tuple(pending),
            merges_checked=checked,
            merges_receipted=receipted,
            probe_age_seconds=probe_age,
            read_at=request.now,
        )


__all__: list[str] = ["HandlerDevDeployReading", "judge_probe", "judge_receipts"]
