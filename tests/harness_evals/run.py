#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Operator-only harness experiment; never imported by product routing.

One call per item, no retries or model judge, no Opus. External adapters are
explicit paths to the operator's installed harness-delegate/lab agent scripts.
Raw receipts stay outside the repository. Emit-ready existing eval run events
and a descriptive results table are written alongside those receipts.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from omnimarket.handlers.handler_harness_eval import (
    ModelHarnessObservation,
    build_harness_run,
    normalize_observation,
    summarize,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerHarnessEvalProcess,
)
from tests.harness_evals.fixtures import (
    fixed_tasks,
    fixture_digest,
    materialize,
)
from tests.harness_evals.recent import check_task as check
from tests.harness_evals.recent import recent_digest, recent_tasks

run_command = HandlerHarnessEvalProcess().handle


def classify(
    receipt: dict[str, object], rc: int, passed: bool
) -> tuple[bool, str | None]:
    error = receipt.get("error") or {}
    error_type = error.get("class", "") if isinstance(error, dict) else str(error)
    if rc in (69, 75, 77, 78) or error_type in (
        "HarnessUnavailable",
        "HarnessNotLoggedIn",
        "HarnessExcluded",
        "HarnessPlacementRefused",
    ):
        return False, str(error_type or "harness-unavailable")
    if passed and rc == 0:
        return True, None
    if rc == 124 or receipt.get("timed_out"):
        return True, "timeout"
    if rc not in (0, 3):
        return True, str(error_type or "harness-error")
    return True, "checker-failed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--harness",
        required=True,
        choices=("codex", "claude-glm", "claude", "local-agent"),
    )
    parser.add_argument("--delegate-script", type=Path, required=True)
    parser.add_argument(
        "--codex-cli",
        action="store_true",
        help="use CLI default model when harness-delegate requires a missing configured model",
    )
    parser.add_argument("--lab-script", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--timeout-s", type=int, default=90)
    parser.add_argument(
        "--tenant-id", type=UUID, required=True, help="operator lab tenant only"
    )
    parser.add_argument("--lane", required=True)
    parser.add_argument("--ticket", required=True)
    parser.add_argument(
        "--task-class",
        choices=tuple(sorted({t["task_class"] for t in fixed_tasks()})),
        help="explicit subset; replacement of invalid fixtures must be reported",
    )
    parser.add_argument("--cohort", choices=("fixed", "recent"), default="fixed")
    parser.add_argument(
        "--item-key",
        action="append",
        help="explicit item subset, for invalid-checker replacement trials",
    )
    args = parser.parse_args()
    if not 30 <= args.timeout_s <= 140:
        parser.error(
            "timeout must fit the installed harness call budget (30..140 seconds)"
        )
    if args.harness == "local-agent" and args.lab_script is None:
        parser.error("--lab-script is required for local-agent")
    args.output.mkdir(parents=True, exist_ok=False)
    suite = fixed_tasks() if args.cohort == "fixed" else recent_tasks()
    if args.item_key and set(args.item_key) - {t["item_key"] for t in suite}:
        parser.error("unknown item key for cohort")
    digest = fixture_digest() if args.cohort == "fixed" else recent_digest()
    tasks = [
        t
        for t in suite
        if (not args.task_class or t["task_class"] == args.task_class)
        and (not args.item_key or t["item_key"] in args.item_key)
    ]
    observations: list[ModelHarnessObservation] = []
    model_default = {
        "claude": "sonnet",
        "claude-glm": "glm-5.3",
        "codex": "configured-default",
        "local-agent": "Qwen3.8-27B",
    }[args.harness]
    configuration = (
        f"fixture-v1:effort-low:timeout-{args.timeout_s}s:adapter-{'cli' if args.codex_cli else 'delegate'}"
        if args.harness != "local-agent"
        else f"fixture-v1:crush:timeout-{args.timeout_s}s"
    )
    if args.cohort == "recent":
        configuration = configuration.replace("fixture-v1", "receipt-v1")
    for task in tasks:
        item_dir = args.output / task["item_key"]
        item_dir.mkdir()
        root = item_dir / "worktree"
        prompt, git_dir = materialize(task, root, args.repo)
        prompt += "\nWork only in this fixture. Do not fetch, push, post comments, weaken hooks, edit tests, or inspect the checker. Write requested outputs to files; prose on stdout alone is insufficient. One attempt only.\n"
        prompt_file = item_dir / "prompt.txt"
        prompt_file.write_text(prompt)
        child_env = {
            **os.environ,
            "GIT_DIR": git_dir,
            "GIT_WORK_TREE": str(root),
            "GIT_EDITOR": ":",
        }
        started = time.monotonic()
        if args.harness == "codex" and args.codex_cli:
            command = [
                "codex",
                "exec",
                "-C",
                str(root),
                "-s",
                "workspace-write",
                "-c",
                'forced_login_method="chatgpt"',
                "-c",
                'model_reasoning_effort="low"',
                "--json",
                "-o",
                str(item_dir / "answer.md"),
                "-",
            ]
        elif args.harness == "local-agent":
            command = [
                sys.executable,
                str(args.lab_script),
                "--worktree",
                str(root),
                "--scratch",
                str(item_dir / "local"),
                "--task-file",
                str(prompt_file),
                "--timeout",
                str(args.timeout_s),
            ]
        else:
            command = [
                sys.executable,
                str(args.delegate_script),
                "run",
                "--harness",
                args.harness,
                "--task-class",
                "code_generation",
                "--effort",
                "low",
                "--writable-repo",
                str(root),
                "--prompt-file",
                str(prompt_file),
                "--check",
                shlex.join(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("fixtures.py")),
                        "--item-key",
                        task["item_key"],
                        "--root",
                        str(root),
                        "--git-dir",
                        git_dir,
                    ]
                ),
                "--out-dir",
                str(item_dir / "receipts"),
                "--timeout-s",
                str(args.timeout_s),
                "--lane",
                args.lane,
                "--ticket",
                args.ticket,
            ]
            # The adapter and the independent post-call fold use the same
            # deterministic checker; a model's prose never decides a pass.
            if args.harness in ("claude", "claude-glm"):
                command += ["--model", model_default]
        rc = run_command(
            command,
            env=child_env,
            timeout=args.timeout_s + 55,
            log=item_dir / "process.log",
            prompt_file=prompt_file if args.codex_cli else None,
        )
        duration = time.monotonic() - started
        receipt: dict[str, object] = {}
        receipts = list((item_dir / "receipts").glob("*/receipt.json"))
        if receipts:
            receipt = json.loads(receipts[0].read_text())
        elif args.harness == "local-agent":
            candidate = item_dir / "local" / "lab-crush-receipt.json"
            if candidate.exists():
                receipt = json.loads(candidate.read_text())
        checker_passed = check(task, root, git_dir=git_dir)
        available, failure_kind = classify(receipt, rc, checker_passed)
        if args.harness == "local-agent" and rc in (3, 4, 5):
            available, failure_kind = False, "local-agent-unavailable"
        reported_model = str(receipt.get("model") or model_default)
        row = ModelHarnessObservation(
            item_key=task["item_key"],
            task_class=task["task_class"],
            harness=args.harness,
            model=reported_model,
            configuration=configuration,
            duration_s=duration,
            available=available,
            passed=available and rc == 0 and checker_passed,
            harness_succeeded=receipt.get("harness_rc") == 0 if receipts else rc == 0,
            reasoning_effort=(receipt.get("config") or {}).get("effort"),
            max_tokens=(receipt.get("context") or {}).get("output"),
            failure_kind=failure_kind,
        )
        transcript = ""
        if receipts and (receipts[0].parent / "transcript.jsonl").exists():
            transcript = (receipts[0].parent / "transcript.jsonl").read_text()
        error = receipt.get("error") or {}
        row = normalize_observation(
            row,
            diagnostic=(item_dir / "process.log").read_text(),
            error_class=str(error.get("class", "")) if isinstance(error, dict) else "",
            transcript=transcript,
        )
        observations.append(row)
        with (args.output / "observations.jsonl").open("a") as stream:
            stream.write(row.model_dump_json() + "\n")
        print(
            f"{args.harness} {row.item_key} pass={row.passed} available={row.available} failure={row.failure_kind} minutes={duration / 60:.3f}",
            flush=True,
        )
    cells = summarize(tuple(observations))
    (args.output / "summary.json").write_text(json.dumps(cells, indent=2) + "\n")
    events = []
    # Model identity can differ for unavailable preflight and actual runs.
    for model in sorted({r.model for r in observations}):
        events.append(
            build_harness_run(
                tuple(r for r in observations if r.model == model),
                tenant_id=args.tenant_id,
                manifest_id=(
                    "harness-lanes-v2:"
                    if args.cohort == "fixed"
                    else "harness-lanes-recent-v1:"
                )
                + digest,
                observed_at=datetime.now(UTC),
            ).model_dump(mode="json")
        )
    (args.output / "eval-runs.json").write_text(json.dumps(events, indent=2) + "\n")
    (args.output / "provenance.json").write_text(
        json.dumps(
            {
                "fixture_sha256": digest,
                "cohort": args.cohort,
                "harness": args.harness,
                "timeout_s": args.timeout_s,
                "calls_per_item": 1,
                "no_opus": True,
                "checker": "harness_eval_fixtures.check",
                "adapter_sha256": __import__("hashlib")
                .sha256(
                    (
                        args.lab_script
                        if args.harness == "local-agent"
                        else args.delegate_script
                    ).read_bytes()
                )
                .hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
