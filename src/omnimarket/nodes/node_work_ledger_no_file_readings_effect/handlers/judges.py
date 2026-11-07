# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seven judgements of plan section 4.3: facts in, WORKING or NOT_WORKING with reasons out.

Each is a pure function of what its probe saw and the overlay's bounds. A reading that cannot be
judged (nothing to compare) is UNKNOWN, never WORKING. Named breaks live in the overlay's
``injected_fault`` of each reading.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_no_file_readings_effect.models import (
    ModelAbsenceFacts,
    ModelCensusFacts,
    ModelFileProbeFacts,
    ModelNoFileOverlay,
    ModelPublishFacts,
    ModelRebuildFacts,
    ModelReceiptFacts,
    ModelRestoreFacts,
)

Judgement = tuple[list[str], bool]  # (reasons the reading is red, judged at all)


def reason_token(text: str) -> str:
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(p for p in out.split("-") if p)[:80] or "unknown"


def judge_file_probe(facts: ModelFileProbeFacts) -> Judgement:
    reasons = [f"file-present-{reason_token(p)}" for p in facts.paths_present]
    if facts.receipt.ledger_seq is None:
        reasons.append("probe-append-has-no-ledger-seq")
    if facts.receipt.file_line:
        reasons.append("probe-append-left-a-file-line")
    if not facts.receipt.read_back:
        reasons.append("probe-row-not-read-back")
    return reasons, True


def judge_census(facts: ModelCensusFacts, overlay: ModelNoFileOverlay) -> Judgement:
    reasons: list[str] = []
    if facts.total_files > overlay.census_total_bound:
        reasons.append(f"census-files-{facts.total_files}")
    reasons += [f"unit-names-path-{reason_token(u)}" for u in facts.units_naming_path]
    return reasons, True


def judge_absence(facts: ModelAbsenceFacts, overlay: ModelNoFileOverlay) -> Judgement:
    reasons: list[str] = []
    if facts.hours_absent < overlay.absence_min_hours:
        reasons.append("path-absent-for-less-than-the-bound")
    if facts.writer_failures:
        reasons.append(f"writer-failures-{facts.writer_failures}")
    if not facts.readers_restarted:
        reasons.append("no-reader-restarted")
    if facts.file_fallback_lines:
        reasons.append(f"file-fallback-lines-{facts.file_fallback_lines}")
    if facts.query_read_exit != 0:
        reasons.append(f"query-read-exit-{facts.query_read_exit}")
    return reasons, True


def judge_rebuild(facts: ModelRebuildFacts) -> Judgement:
    if not facts.live and not facts.rebuilt:
        return ["no-rows-to-compare"], False
    live = {r.seq: r.digest for r in facts.live}
    rebuilt = {r.seq: r.digest for r in facts.rebuilt}
    reasons: list[str] = []
    missing = sorted(set(live) - set(rebuilt))
    extra = sorted(set(rebuilt) - set(live))
    differ = sorted(s for s in set(live) & set(rebuilt) if live[s] != rebuilt[s])
    if missing:
        reasons.append(f"rebuilt-table-lacks-seq-{missing[0]}-of-{len(missing)}")
    if extra:
        reasons.append(f"rebuilt-table-has-extra-seq-{extra[0]}-of-{len(extra)}")
    if differ:
        reasons.append(f"row-differs-at-seq-{differ[0]}-of-{len(differ)}")
    return reasons, True


def judge_restore(facts: ModelRestoreFacts) -> Judgement:
    reasons: list[str] = []
    if not facts.ran:
        reasons.append("restore-drill-not-run")
    if not facts.checksum_ok:
        reasons.append("snapshot-checksum-failed")
    if facts.restored_rows != facts.snapshot_rows:
        reasons.append(f"restored-rows-{facts.restored_rows}-of-{facts.snapshot_rows}")
    return reasons, True


def judge_publish(facts: ModelPublishFacts, overlay: ModelNoFileOverlay) -> Judgement:
    reasons: list[str] = []
    if facts.job_listed:
        reasons.append("publish-job-still-listed")
    age = facts.hours_since_rows_through_commit
    if age is not None and age < overlay.publish_quiet_hours:
        reasons.append("rows-through-commit-inside-the-quiet-window")
    return reasons, True


def judge_receipt(facts: ModelReceiptFacts, overlay: ModelNoFileOverlay) -> Judgement:
    reasons: list[str] = []
    wanted = set(overlay.receipt_sources)
    got = set(facts.compared)
    for source in sorted(got - wanted):
        reasons.append(f"receipt-compares-extra-source-{reason_token(source)}")
    for source in sorted(wanted - got):
        reasons.append(f"receipt-lacks-source-{reason_token(source)}")
    if not facts.exact:
        reasons.append("receipt-not-exact")
    if facts.mismatches:
        reasons.append(f"receipt-mismatches-{facts.mismatches}")
    return reasons, True
