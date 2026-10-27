#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Collect content-free harness proof; retain raw transcripts only in lab state.

An explicit replacement directory supersedes matching item keys. This is for
invalid fixture corrections only; the report must disclose those exclusions.
The collector never launches a model, writes a ledger or publishes an event.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from omnimarket.handlers.handler_harness_eval import (
    ModelHarnessObservation,
    build_harness_capability_run,
    build_harness_run,
    normalize_observation,
    summarize,
)
from omnimarket.models.model_engine_experiment import (
    ModelEngineExperimentRow,
    format_experiment_line,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_projection_delegation_capability_map import (
    HandlerProjectionDelegationCapabilityMap,
)
from tests.harness_evals.fixtures import fixed_tasks, fixture_digest
from tests.harness_evals.recent import recent_digest, recent_tasks


def load_trials(directory: Path) -> list[ModelHarnessObservation]:
    rows = []
    for line in (directory / "observations.jsonl").read_text().splitlines():
        raw = ModelHarnessObservation.model_validate_json(line)
        item_dir = directory / raw.item_key
        receipts = list((item_dir / "receipts").glob("*/receipt.json"))
        receipt = json.loads(receipts[0].read_text()) if receipts else {}
        transcript_path = receipts[0].parent / "transcript.jsonl" if receipts else None
        transcript = (
            transcript_path.read_text()
            if transcript_path and transcript_path.exists()
            else ""
        )
        local_path = item_dir / "local" / "lab-crush-receipt.json"
        local_receipt = (
            json.loads(local_path.read_text()) if local_path.exists() else {}
        )
        effective = receipt.get("config") or {}
        raw = raw.model_copy(
            update={
                "reasoning_effort": effective.get("effort"),
                "max_tokens": (local_receipt.get("context") or {}).get("output"),
            }
        )
        error = receipt.get("error") or {}
        normalized = normalize_observation(
            raw,
            diagnostic=(item_dir / "process.log").read_text(),
            error_class=error.get("class", "") if isinstance(error, dict) else "",
            transcript=transcript,
        )
        # The first run's adapter label omitted a suffix; verified effective
        # parameters are the same low-effort, 90-second delegate configuration.
        if normalized.configuration == "fixture-v1:effort-low:timeout-90s":
            normalized = normalized.model_copy(
                update={
                    "configuration": "fixture-v1:effort-low:timeout-90s:adapter-delegate"
                }
            )
        rows.append(normalized)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trial", type=Path, action="append", required=True)
    parser.add_argument("--replace", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tenant-id", type=UUID, required=True)
    parser.add_argument("--lane", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--harness-versions", type=Path, required=True)
    parser.add_argument("--cohort", choices=("fixed", "recent"), default="fixed")
    parser.add_argument(
        "--attempt-offset",
        type=int,
        default=0,
        help="reserve earlier attempt numbers when publishing another cohort for this lane",
    )
    parser.add_argument(
        "--replacement-reason",
        default="invalid-fixture: binds_ac key omitted from original prompt",
    )
    args = parser.parse_args()
    if args.attempt_offset < 0:
        parser.error("attempt offset must be nonnegative")
    suite = fixed_tasks() if args.cohort == "fixed" else recent_tasks()
    digest = fixture_digest() if args.cohort == "fixed" else recent_digest()
    harness_revision = hashlib.sha256(args.harness_versions.read_bytes()).hexdigest()
    by_key: dict[tuple[str, str], ModelHarnessObservation] = {}
    proofs = []
    excluded = []
    for directory, replacement in [(p, False) for p in args.trial] + [
        (p, True) for p in args.replace
    ]:
        for row in load_trials(directory):
            key = (row.harness, row.item_key)
            if key in by_key:
                if not replacement:
                    raise ValueError(f"duplicate trial {key}")
                excluded.append(
                    {
                        "harness": row.harness,
                        "item_key": row.item_key,
                        "reason": args.replacement_reason,
                    }
                )
            elif replacement:
                raise ValueError(f"replacement without original {key}")
            by_key[key] = row
        proof_files = [
            directory / "observations.jsonl",
            directory / "provenance.json",
            *directory.glob("*/process.log"),
            *directory.glob("*/receipts/*/receipt.json"),
            *directory.glob("*/local/lab-crush-receipt.json"),
        ]
        proofs.append(
            {
                "run": directory.name,
                "files": [
                    {
                        "path": str(p.relative_to(directory)),
                        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                    }
                    for p in sorted(proof_files)
                ],
            }
        )
    expected = {t["item_key"] for t in suite}
    for harness in {r.harness for r in by_key.values()}:
        if {r.item_key for r in by_key.values() if r.harness == harness} != expected:
            raise ValueError(f"incomplete fixed set for {harness}")
    rows = tuple(by_key[key] for key in sorted(by_key))
    args.output.mkdir(parents=True, exist_ok=True)

    def write(name: str, value: object) -> None:
        (args.output / name).write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n"
        )

    write("observations.json", [r.model_dump(mode="json") for r in rows])
    write("summary.json", summarize(rows))
    write(
        "source-proofs.json",
        {
            "runs": proofs,
            "excluded_trials": excluded,
            "fixture_sha256": digest,
            "cohort": args.cohort,
            "harness_versions_sha256": harness_revision,
        },
    )
    now = datetime.now(UTC)
    groups = defaultdict(list)
    for row in rows:
        groups[(row.harness, row.model, row.configuration)].append(row)
    events = [
        build_harness_run(
            tuple(group),
            tenant_id=args.tenant_id,
            manifest_id=(
                "harness-lanes-v2:"
                if args.cohort == "fixed"
                else "harness-lanes-recent-v1:"
            )
            + digest,
            observed_at=now,
        ).model_dump(mode="json")
        for _, group in sorted(groups.items())
    ]
    write("eval-runs.json", events)
    capability = build_harness_capability_run(
        rows,
        tenant_id=args.tenant_id,
        observed_at=now,
        suite_revision=digest,
        harness_revision=harness_revision,
    )
    write("capability-map-run.json", capability.model_dump(mode="json"))
    write(
        "capability-cells.json",
        HandlerProjectionDelegationCapabilityMap()
        .handle(capability)
        .model_dump(mode="json"),
    )
    engine = {
        "local-agent": "local-agent",
        "claude-glm": "glm",
        "claude": "sonnet",
        "codex": "codex",
    }
    experiments = [
        ModelEngineExperimentRow(
            lane=args.lane,
            ticket=args.ticket,
            attempt=args.attempt_offset + index + 1,
            engine=engine[r.harness],
            model=r.model,
            outcome="done"
            if r.passed
            else "failed"
            if r.available
            else "blocked-external",
            failure_cause=r.failure_kind or "none",
            duration_s=round(r.duration_s),
            next_engine="none",
            harness=r.harness,
            task_class=r.task_class,
            configuration=r.configuration,
            item_key=r.item_key,
        )
        for index, r in enumerate(rows)
    ]
    write("experiment-rows.json", [r.model_dump(mode="json") for r in experiments])
    # Proposed rows only. The launching host alone may append these.
    (args.output / "experiment-status.txt").write_text(
        "\n".join(
            format_experiment_line(r, now.strftime("%Y-%m-%dT%H:%M:%SZ"))
            for r in experiments
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
