# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The code eval suite's admission rule, run both ways (OMN-24562).

A suite task is kept only if, run the way a model is graded, its known-good answer passes and an empty
answer fails. ``admit`` reads one candidate through the run leg's suite reader, then scores the held
answer (the control) and an empty draft (a model arm that sent nothing) through the same score request
builder and the same score handler the grader uses. ``static_refusal`` names a check that needs the
network, a live host or a file the model may not write before anything runs.

Run on a host that serves the registry clones::

    python -m tests.nodes.node_code_eval_trial_orchestrator.suite_admission --repos omnimarket,omnibase_infra \\
        --scratch-root <dir> --workers 8 --shard 0/4 --out <file.json>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

import yaml

from omnimarket.models.code_eval_trial.model_code_eval_reduce_io import (
    ModelScoreToSend,
)
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalCandidate,
)
from omnimarket.models.code_eval_trial.model_code_eval_task_material import (
    ModelCodeEvalTaskMaterial,
)
from omnimarket.models.eval_suite_census.committed_suite import load_committed_suite
from omnimarket.models.eval_suite_census.model_suite_task import ModelCodeEvalSuiteTask
from omnimarket.models.model_code_score import ModelCodeScoreOverlay
from omnimarket.nodes.node_code_contract_score_effect.handlers.handler_code_contract_score import (
    HandlerCodeContractScore,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.builders import (
    build_score_request,
)
from omnimarket.nodes.node_code_eval_trial_orchestrator.orchestration.suite_reader import (
    GitCodeEvalSuiteReader,
)

# a check value that reaches outside the worktree: the network, a live host or a cluster
_OUTSIDE = re.compile(
    r"(https?://|\bgh\s+(api|pr|run|workflow|repo)\b|\bcurl\b|\bwget\b|\bssh\b|\bscp\b|"
    r"\bkubectl\b|\bpsql\b|\bdocker\s+(compose|exec|run|ps)\b|\bsystemctl\b|\baws\b)"
)


@dataclass(frozen=True)
class Admission:
    """One task's admission: both verdicts, the time both runs took, and why it was refused."""

    repository: str
    contract_id: str
    known_good: str
    empty: str
    seconds: float
    refused: str | None

    @property
    def admitted(self) -> bool:
        return self.refused is None


def static_refusal(
    material_checks: Sequence[tuple[str, str]],
    writable: Sequence[str],
    protected: Sequence[str],
) -> str | None:
    """Why a task's checks cannot run in a worktree alone, or None.

    ``material_checks`` is (check_type, check_value) pairs; ``writable`` the held paths the model may
    write; ``protected`` the overlay's protected prefixes. A held answer that touches a protected path
    is a file the model may not write.
    """
    for check_type, check_value in material_checks:
        if _OUTSIDE.search(check_value):
            return f"check_needs_outside:{check_type}"
    for path in writable:
        for prefix in protected:
            if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
                return "answer_needs_protected_path"
    return None


def verdict_of(
    overlay: ModelCodeScoreOverlay,
    send: ModelScoreToSend,
    material: ModelCodeEvalTaskMaterial,
) -> str:
    """The verdict the grader gives one draft; an unscorable verdict reads ``unscorable(<source>)``."""
    request = build_score_request(uuid4(), send, material, overlay)
    result = asyncio.run(HandlerCodeContractScore().handle(request))
    if result.scored is None:
        return f"refused:{result.failed.code.value if result.failed else 'none'}"
    verdict = result.scored.verdict
    if verdict.source is not None:
        return f"{verdict.checks.value}({verdict.source.value})"
    return verdict.checks.value


def admit(
    candidate: ModelCodeEvalCandidate,
    overlay: ModelCodeScoreOverlay,
    recorded: Sequence[ModelCodeEvalSuiteTask] = (),
) -> Admission:
    """Run one candidate both ways; admitted only when the known good passes and the empty fails."""
    reader = GitCodeEvalSuiteReader(
        score_overlay=overlay, max_task_chars=20_000, recorded_tasks=recorded
    )
    (material,) = reader.read((candidate,))
    started = time.monotonic()

    def row(known: str, empty: str, refused: str | None) -> Admission:
        return Admission(
            candidate.repository,
            candidate.contract_id,
            known,
            empty,
            round(time.monotonic() - started, 1),
            refused,
        )

    if material.excluded_reason is not None:
        return row("none", "none", f"reader:{material.excluded_reason}")
    task = next(
        (
            t
            for t in recorded
            if (t.repository, t.contract_id)
            == (candidate.repository, candidate.contract_id)
        ),
        None,
    )
    pairs = (
        [(c.check_type, c.check_value) for c in task.checks] if task is not None else []
    )
    static = static_refusal(
        pairs,
        material.writable_globs,
        overlay.protected_paths.get(candidate.repository, ()),
    )
    if static is not None:
        return row("none", "none", static)
    if not material.held_diff.strip():
        return row("none", "none", "no_held_diff")
    key = material.task_key
    known = verdict_of(
        overlay,
        ModelScoreToSend(task_key=key, model_arm="", context_arm="off", try_ordinal=1),
        material,
    )
    empty = verdict_of(
        overlay,
        ModelScoreToSend(
            task_key=key,
            model_arm="empty",
            context_arm="off",
            try_ordinal=1,
            draft_diff="",
        ),
        material,
    )
    refused = None
    if known != "pass":
        refused = f"known_good_{known}"
    elif empty == "pass":
        refused = "empty_passes"
    return row(known, empty, refused)


def load_overlay(path: Path, scratch_root: Path) -> ModelCodeScoreOverlay:
    """The deployed score overlay with the scratch root moved to this run's own directory."""
    fields = yaml.safe_load(path.read_text(encoding="utf-8"))
    fields["scratch_root"] = str(scratch_root)
    return ModelCodeScoreOverlay.model_validate(fields)


def table(rows: Sequence[Admission]) -> str:
    lines = [f"{'task':<34} {'known good':<24} {'empty':<20} seconds  refused"]
    for r in rows:
        lines.append(
            f"{r.repository + ':' + r.contract_id:<34} {r.known_good:<24} {r.empty:<20} "
            f"{r.seconds:>7}  {r.refused or ''}"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", default="omnimarket,omnibase_infra")
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--shard", default="0/1")
    parser.add_argument("--max-files", type=int, default=4)
    parser.add_argument("--only", default="", help="comma list of contract ids")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    index, total = (int(x) for x in args.shard.split("/"))
    repos = set(args.repos.split(","))
    only = {x for x in args.only.split(",") if x}
    suite = load_committed_suite().tasks
    picked = [
        t
        for t in suite
        if t.repository in repos
        and (t.contract_id in only if only else t.files_changed <= args.max_files)
    ]
    picked = [
        t
        for i, t in enumerate(sorted(picked, key=lambda t: t.merge_commit))
        if i % total == index
    ]
    overlay = load_overlay(args.overlay, args.scratch_root)

    def run(task: ModelCodeEvalSuiteTask) -> Admission:
        candidate = ModelCodeEvalCandidate(
            repository=task.repository,
            merge_commit=task.merge_commit,
            contract_id=task.contract_id,
        )
        return admit(candidate, overlay, suite)

    rows: list[Admission] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(run, t) for t in picked]):
            done = future.result()
            rows.append(done)
            print(table([done]).splitlines()[-1], flush=True)
    args.out.write_text(
        json.dumps([asdict(r) for r in rows], indent=1), encoding="utf-8"
    )
    print(table(rows), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
