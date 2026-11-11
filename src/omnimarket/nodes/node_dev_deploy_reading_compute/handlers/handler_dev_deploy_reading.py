# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDevDeployReading: piece 11's reading at a read time (OMN-22958).

Two rules, both judged against the request's own time and thresholds:

* receipts: every runtime-affecting merge older than the deadline has a PASS lab pass receipt
  for its exact sha, on the required lane, finished no later than ``merged_at + deadline``. A merge
  inside its deadline is pending and fails nothing. A receipt for another sha is no evidence.
* probe: the newest functional probe of the running runtime is not future-dated, is inside its max
  age, passed, and observed the exact sha it was asked for.

Lineage (ruling 2026-11-10T16:53:27Z), when the overlay's rule allows it, widens each rule by ancestry the clone read:

* receipts: a PASS receipt whose sha has the merge as an ancestor covers the merge, within the rule's commit and
  time bounds. A receipt that is not a descendant covers nothing; an unreadable pair is unread, never a pass.
* probe: a running sha that descends from the expected one passes when no runtime-affecting commit lies between
  them and the distance is inside the rule's bound.

A failure of a readable source reads NOT_WORKING. A source that could not be read reads UNKNOWN only
when nothing readable already fails.
"""

from __future__ import annotations

import math
from datetime import timedelta

from omnimarket.models.model_dev_deploy_reading import (
    ModelDevDeployReading,
    ModelDevDeployReadingRequest,
    ModelLabPassReceiptFact,
    ModelLineageFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.models.model_reading import EnumReadingVerdict

_SHORT = 12
#: A pair the clone could not read is unread, not failed: the handler moves these out of the failures.
RECEIPT_LINEAGE_UNREAD = "receipt-lineage-unread:"
PROBE_LINEAGE_UNREAD = "probe-lineage-unread:"
_UNREAD_PREFIXES = (RECEIPT_LINEAGE_UNREAD, PROBE_LINEAGE_UNREAD)
_NOT_READ = "no lineage fact was read for the pair"


def _short(sha: str) -> str:
    return sha[:_SHORT]


def _lineage_fact(
    request: ModelDevDeployReadingRequest, base_sha: str, head_sha: str
) -> ModelLineageFact | None:
    for fact in request.lineage:
        if fact.base_sha == base_sha and fact.head_sha == head_sha:
            return fact
    return None


def _covering_receipt(
    request: ModelDevDeployReadingRequest,
    merge: ModelRuntimeMerge,
    lane_passes: list[ModelLabPassReceiptFact],
) -> tuple[bool, str | None]:
    """Whether a later PASS receipt covers ``merge`` by ancestry, and why a pair stayed unread."""
    rule = request.lineage_rule
    if not rule.receipt_covers_descendant:
        return False, None
    window = timedelta(seconds=rule.receipt_cover_window_seconds)
    unread: str | None = None
    for later in lane_passes:
        if later.sha == merge.sha or not (
            timedelta(0) <= later.finished_at - merge.merged_at <= window
        ):
            continue
        fact = _lineage_fact(request, merge.sha, later.sha)
        if fact is None:
            unread = unread or _NOT_READ
        elif fact.relation == "unread":
            unread = unread or fact.unread_reason
        elif (
            fact.relation == "ancestor"
            and fact.commits_between is not None
            and fact.commits_between <= rule.max_commits_between
        ):
            return True, None
    return False, unread


def judge_receipts(
    request: ModelDevDeployReadingRequest,
) -> tuple[list[str], list[str], int, int]:
    """The failures, the pending merges, the merges checked and the merges receipted."""
    deadline = timedelta(seconds=request.receipt_deadline_seconds)
    by_sha: dict[str, list[ModelLabPassReceiptFact]] = {}
    for receipt in request.receipts:
        if receipt.lane == request.required_lane:
            by_sha.setdefault(receipt.sha, []).append(receipt)
    lane_passes = [
        r
        for r in request.receipts
        if r.lane == request.required_lane
        and r.result == "PASS"
        and r.finished_at <= request.now
    ]
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
        own = by_sha.get(merge.sha, [])
        passes = sorted(
            r.finished_at
            for r in own
            if r.result == "PASS" and r.finished_at <= request.now
        )
        in_time = [p for p in passes if timedelta(0) <= p - merge.merged_at <= deadline]
        if in_time:
            receipted += 1
            continue
        covered, unread_reason = _covering_receipt(request, merge, lane_passes)
        if covered:
            receipted += 1
            continue
        if unread_reason is not None and age > deadline:
            failures.append(f"{RECEIPT_LINEAGE_UNREAD}{tag}:{unread_reason}")
            continue
        if age > deadline and passes:
            after_merge = [p for p in passes if p >= merge.merged_at]
            if after_merge:
                late = int((after_merge[0] - merge.merged_at).total_seconds())
                failures.append(
                    f"receipt-late:{tag}:{late}s-over-{request.receipt_deadline_seconds}s"
                )
            else:
                failures.append(f"receipt-before-merge:{tag}")
            continue
        if age <= deadline:
            pending.append(f"receipt-pending:{tag}")
            continue
        failures.append(
            f"no-pass-receipt:{tag}:age-{int(age.total_seconds())}s-over-{request.receipt_deadline_seconds}s"
        )
        if any(r.result == "FAIL" for r in own):
            failures.append(f"receipt-fail:{tag}")
    return failures, pending, checked, receipted


def _probe_sha_failures(
    request: ModelDevDeployReadingRequest, probe: ModelRuntimeProbeFact, observed: str
) -> list[str]:
    """The failures of a probe whose observed sha is not the expected one; none when a descendant is accepted."""
    mismatch = (
        f"probe-sha-mismatch:{probe.probe_id}:{_short(probe.expected_sha)}"
        f"-observed-{_short(observed)}"
    )
    rule = request.lineage_rule
    if not rule.probe_accepts_descendant:
        return [mismatch]
    fact = _lineage_fact(request, probe.expected_sha, observed)
    if fact is None:
        return [f"{PROBE_LINEAGE_UNREAD}{probe.probe_id}:{_NOT_READ}"]
    if fact.relation == "unread":
        return [f"{PROBE_LINEAGE_UNREAD}{probe.probe_id}:{fact.unread_reason}"]
    if fact.relation == "not-ancestor":
        return [mismatch]
    if fact.runtime_affecting_between:
        return [
            f"probe-runtime-commit-between:{probe.probe_id}:{_short(probe.expected_sha)}"
            f"-observed-{_short(observed)}:{len(fact.runtime_affecting_between)}"
        ]
    if (
        fact.commits_between is not None
        and fact.commits_between > rule.max_commits_between
    ):
        return [
            f"probe-descendant-too-far:{probe.probe_id}:{fact.commits_between}"
            f"-over-{rule.max_commits_between}"
        ]
    return []


def judge_probe(
    request: ModelDevDeployReadingRequest,
) -> tuple[list[str], int | None]:
    """The failures of the newest probe and its age, none when there is no probe to age."""
    if not request.probes:
        return ["probe-missing"], None
    newest: ModelRuntimeProbeFact = max(
        request.probes, key=lambda p: (p.probed_at, p.probe_id)
    )
    exact_age = request.now - newest.probed_at
    if exact_age < timedelta(0):
        return [f"probe-future-dated:{newest.probe_id}"], None
    failures: list[str] = []
    if exact_age > timedelta(seconds=request.probe_max_age_seconds):
        failures.append(
            f"probe-stale:{newest.probe_id}:age-{math.ceil(exact_age.total_seconds())}s"
            f"-over-{request.probe_max_age_seconds}s"
        )
    if not newest.ok:
        failures.append(f"probe-failed:{newest.probe_id}")
    if newest.observed_sha is None:
        failures.append(f"probe-sha-unobserved:{newest.probe_id}")
    elif newest.observed_sha != newest.expected_sha:
        failures += _probe_sha_failures(request, newest, newest.observed_sha)
    return failures, int(exact_age.total_seconds())


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
        unread += [f for f in failures if f.startswith(_UNREAD_PREFIXES)]
        failures = [f for f in failures if not f.startswith(_UNREAD_PREFIXES)]
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
