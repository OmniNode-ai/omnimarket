# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runner for the delegation task-complexity ladder (OMN-18300).

Feeds each committed bundle to one delegation surface exactly once, scores the
response mechanically, and emits a JSON result table.

**The delegated work is never retried.** If a delegation fails, refuses, or comes
back empty, that is the result for that task on that path. The harness retries
only its own plumbing -- reading a file it just wrote -- and nothing that would
give the model a second attempt at the same prompt.

**Paid escalation is disabled on the local path** (``ONEX_DELEGATION_ALLOW_PAID=0``).
The operator asked what the LOCAL model can do; leaving escalation on would
measure the escalation chain and bill for the privilege. When the local tier's
answer is refused by the quality gate, the harness recovers the rejected text
from the run's own capture log and scores it anyway, so the table can report the
two questions separately: did the gate accept it, and was it actually right.
That distinction is the whole of OMN-18295.

Usage::

    python benchmarks/delegation_ladder/run_ladder.py --path local
    python benchmarks/delegation_ladder/run_ladder.py --path cloud --api-key-file <file>
    python benchmarks/delegation_ladder/run_ladder.py --path local --only R4 R6
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from benchmarks.delegation_ladder.complexity import classify  # noqa: E402
from benchmarks.delegation_ladder.models import (  # noqa: E402
    EnumOutcome,
    EnumPath,
    EnumScorerKind,
    ModelRunRecord,
    ModelScore,
    ModelTaskBundle,
)
from benchmarks.delegation_ladder.scorers import (  # noqa: E402
    score_diagnosis_location,
    score_exact_match,
    score_grounding_coverage,
    score_grounding_rubric,
    score_patch_apply_and_test,
    score_unit_test_execution,
    sha256_of,
    split_answer,
)

BUNDLES = HERE / "bundles"
FIXTURES = HERE / "fixtures"
RESULTS = HERE / "results"


@dataclass(frozen=True)
class Invocation:
    """What one delegation surface gave back, before any scoring."""

    response: str
    delegation_id: str | None
    tier: str | None
    backend_id: str | None
    model_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    cost_usd: float | None
    model_latency_ms: int | None
    quality_gate_passed: bool | None
    quality_score: float | None
    wall_ms: int
    refused: bool
    notes: str


# --------------------------------------------------------------------------
# Local path
# --------------------------------------------------------------------------

_REJECTED_RE = re.compile(
    r"rejected candidate content \(task_type=\S+ tier=(?P<tier>\S+) "
    r"correlation=(?P<cid>[0-9a-f-]+) reason=(?P<reason>[^)]*)\):\n(?P<body>.*?)"
    r"(?=\n\d\d:\d\d:\d\d \s*\w+\s+|\Z)",
    re.DOTALL,
)


def _recover_rejected(state_root: Path) -> tuple[str, str, str] | None:
    """Last gate-rejected candidate from the run's own capture log.

    This is not a second attempt. It reads text the model already produced in
    the single run that was made, which the CLI discards because the gate
    refused it. Without this the local path would report an empty response for
    every gate rejection and the table could not distinguish "the model was
    wrong" from "the gate said no".
    """
    captures = state_root / "captures"
    if not captures.is_dir():
        return None
    logs = sorted(captures.glob("*.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    text = logs[-1].read_text(encoding="utf-8", errors="replace")
    matches = list(_REJECTED_RE.finditer(text))
    if not matches:
        return None
    last = matches[-1]
    return last.group("body").strip(), last.group("tier"), last.group("reason")


def invoke_local(bundle: ModelTaskBundle, onex: Path, timeout_s: int) -> Invocation:
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        raise RuntimeError("OMNI_HOME must be set; there is no default (rule 8)")
    env = dict(os.environ)
    env["OMNI_HOME"] = omni_home
    env["ONEX_DELEGATION_ALLOW_PAID"] = "0"
    env.pop("ONEX_EVENT_BUS_TYPE", None)
    env["PATH"] = (
        f"{Path(omni_home) / 'omnibase_infra' / '.venv' / 'bin'}:{env['PATH']}"
    )

    tmp = Path(tempfile.mkdtemp(prefix=f"ladder-{bundle.task_id}-"))
    try:
        started = time.monotonic()
        completed = subprocess.run(
            [
                "bash",
                str(onex),
                "delegate",
                bundle.prompt,
                "--task-type",
                bundle.task_type,
                "--state-root",
                str(tmp),
                "--timeout",
                str(timeout_s),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_s + 180,
            env=env,
            check=False,
        )
        wall_ms = int((time.monotonic() - started) * 1000)

        receipt: dict[str, object] | None = None
        for line in completed.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    receipt = json.loads(stripped)
                    break
                except json.JSONDecodeError:
                    continue
        if receipt is None and completed.stdout.strip().startswith("{"):
            try:
                receipt = json.loads(completed.stdout)
            except json.JSONDecodeError:
                receipt = None

        result = receipt.get("result") if receipt is not None else None
        if isinstance(result, dict):
            raw_attempts = result.get("attempts")
            attempts: list[dict[str, object]] = (
                [a for a in raw_attempts if isinstance(a, dict)]
                if isinstance(raw_attempts, list)
                else []
            )
            accepted: dict[str, object] = next(
                (a for a in attempts if a.get("acceptance_decision") == "accept"),
                attempts[-1] if attempts else {},
            )
            raw_metrics = result.get("metrics")
            metrics: dict[str, object] = (
                raw_metrics if isinstance(raw_metrics, dict) else {}
            )
            return Invocation(
                response=str(result.get("response") or ""),
                delegation_id=str(result.get("correlation_id") or "") or None,
                tier=_as_str(accepted.get("tier")),
                backend_id=_as_str(accepted.get("backend_id")),
                model_id=_as_str(accepted.get("model_id"))
                or _as_str(result.get("model_name")),
                input_tokens=_as_int(metrics.get("input_tokens")),
                output_tokens=_as_int(metrics.get("output_tokens")),
                total_tokens=_as_int(metrics.get("total_tokens")),
                cost_usd=_as_float(metrics.get("cost_usd")),
                model_latency_ms=_as_int(metrics.get("latency_ms")),
                quality_gate_passed=_as_bool(result.get("quality_gate_passed")),
                quality_score=_as_float(result.get("quality_score")),
                wall_ms=wall_ms,
                refused=False,
                notes=f"attempts={len(attempts)}",
            )

        recovered = _recover_rejected(tmp)
        if recovered is not None:
            body, tier, reason = recovered
            return Invocation(
                response=body,
                delegation_id=None,
                tier=tier,
                backend_id="local-heavy-reasoning",
                model_id=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost_usd=0.0,
                model_latency_ms=None,
                quality_gate_passed=False,
                quality_score=None,
                wall_ms=wall_ms,
                refused=False,
                notes=(
                    "the quality gate refused every attempt and paid escalation is "
                    f"disabled; text recovered from this run's capture log. reason={reason}"
                ),
            )

        tail = (completed.stderr or completed.stdout).strip()[-400:]
        return Invocation(
            response="",
            delegation_id=None,
            tier=None,
            backend_id=None,
            model_id=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cost_usd=None,
            model_latency_ms=None,
            quality_gate_passed=None,
            quality_score=None,
            wall_ms=wall_ms,
            refused=True,
            notes=f"no receipt and no recoverable candidate; rc={completed.returncode}; {tail}",
        )
    except subprocess.TimeoutExpired:
        return Invocation(
            response="",
            delegation_id=None,
            tier=None,
            backend_id=None,
            model_id=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cost_usd=None,
            model_latency_ms=None,
            quality_gate_passed=None,
            quality_score=None,
            wall_ms=(timeout_s + 180) * 1000,
            refused=True,
            notes=f"client exceeded {timeout_s + 180}s",
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Cloud path
# --------------------------------------------------------------------------


def invoke_cloud(
    bundle: ModelTaskBundle,
    onex: Path,
    api_key_file: Path | None,
    timeout_s: int,
    base_url: str | None = None,
) -> Invocation:
    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        raise RuntimeError("OMNI_HOME must be set; there is no default (rule 8)")
    env = dict(os.environ)
    env["PATH"] = (
        f"{Path(omni_home) / 'omnibase_infra' / '.venv' / 'bin'}:{env['PATH']}"
    )
    env.pop("ONEX_EVENT_BUS_TYPE", None)

    outdir = Path(tempfile.mkdtemp(prefix=f"ladder-cloud-{bundle.task_id}-"))
    argv = [
        "bash",
        str(onex),
        "cloud",
        "delegate",
        bundle.prompt,
        "--task-type",
        bundle.task_type,
        "--output-dir",
        str(outdir),
        "--timeout",
        str(timeout_s),
    ]
    if api_key_file is not None:
        argv += ["--api-key-file", str(api_key_file)]
    if base_url:
        argv += ["--base-url", base_url]

    try:
        started = time.monotonic()
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout_s + 180,
            env=env,
            check=False,
        )
        wall_ms = int((time.monotonic() - started) * 1000)

        receipt: dict[str, object] = {}
        response = ""
        for path in sorted(outdir.rglob("*")):
            if path.is_file() and path.suffix == ".json":
                try:
                    blob = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(blob, dict):
                    receipt = {**receipt, **blob}
            elif path.is_file() and path.suffix in {".txt", ".md"}:
                response = response or path.read_text(encoding="utf-8")

        if not response:
            response = completed.stdout.strip()

        if completed.returncode != 0 and not response:
            tail = (completed.stderr or completed.stdout).strip()[-500:]
            return Invocation(
                response="",
                delegation_id=str(receipt.get("workflow_id") or "") or None,
                tier=None,
                backend_id=None,
                model_id=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost_usd=None,
                model_latency_ms=None,
                quality_gate_passed=None,
                quality_score=None,
                wall_ms=wall_ms,
                refused=True,
                notes=f"gateway refused or failed; rc={completed.returncode}; {tail}",
            )

        return Invocation(
            response=response,
            delegation_id=str(
                receipt.get("workflow_id") or receipt.get("correlation_id") or ""
            )
            or None,
            tier=_as_str(_dig(receipt, "tier")),
            backend_id=_as_str(_dig(receipt, "backend_id")),
            model_id=_as_str(
                _dig(receipt, "model_used", "terminal_model_used", "model")
            ),
            input_tokens=_dig_int(receipt, "input_tokens", "prompt_tokens"),
            output_tokens=_dig_int(receipt, "output_tokens", "completion_tokens"),
            total_tokens=_dig_int(receipt, "total_tokens", "terminal_total_tokens"),
            cost_usd=_dig_float(receipt, "cost_usd"),
            model_latency_ms=_dig_int(receipt, "latency_ms", "terminal_latency_ms"),
            quality_gate_passed=_as_bool(_dig(receipt, "quality_gate_passed")),
            quality_score=_dig_float(receipt, "quality_score", "actual_score"),
            wall_ms=wall_ms,
            refused=False,
            notes=(
                f"rc={completed.returncode} status={_dig(receipt, 'terminal_status', 'status')}"
                + (
                    f" failure_class={_dig(receipt, 'failure_class', 'terminal_failure_class')}"
                    f" reason={str(_dig(receipt, 'failure_reason', 'terminal_failure_reason'))[:200]}"
                    if _dig(receipt, "failure_class", "terminal_failure_class")
                    else ""
                )
            ),
        )
    except subprocess.TimeoutExpired:
        return Invocation(
            response="",
            delegation_id=None,
            tier=None,
            backend_id=None,
            model_id=None,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            cost_usd=None,
            model_latency_ms=None,
            quality_gate_passed=None,
            quality_score=None,
            wall_ms=(timeout_s + 180) * 1000,
            refused=True,
            notes=f"client exceeded {timeout_s + 180}s",
        )
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def _as_str(value: object) -> str | None:
    """Narrow one field out of an untyped receipt. None when it is not a string."""
    return value if isinstance(value, str) else None


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_int(value: object) -> int | None:
    return (
        int(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _as_float(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def _dig(blob: object, *keys: str) -> object:
    """First value found under any of ``keys``, at any depth. None if absent.

    The gateway's saved run shape is not pinned by this harness, so the fields
    are located by name rather than by a path that would silently read None if
    the layout moved.
    """
    stack: list[object] = [blob]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key in keys:
                if key in node and node[key] is not None:
                    return node[key]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None


def _dig_int(blob: object, *keys: str) -> int | None:
    value = _dig(blob, *keys)
    return int(value) if isinstance(value, (int, float)) else None


def _dig_float(blob: object, *keys: str) -> float | None:
    value = _dig(blob, *keys)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------
# Scoring dispatch
# --------------------------------------------------------------------------


def score(bundle: ModelTaskBundle, response: str) -> ModelScore:
    config = dict(bundle.scorer_config)
    match bundle.scorer:
        case EnumScorerKind.GROUNDING_RUBRIC:
            return score_grounding_rubric(response, config)
        case EnumScorerKind.GROUNDING_COVERAGE:
            return score_grounding_coverage(response, config)
        case EnumScorerKind.EXACT_MATCH:
            return score_exact_match(response, config)
        case EnumScorerKind.UNIT_TEST_EXECUTION:
            return score_unit_test_execution(response, config, FIXTURES)
        case EnumScorerKind.DIAGNOSIS_LOCATION:
            return score_diagnosis_location(response, config)
        case EnumScorerKind.PATCH_APPLY_AND_TEST:
            return score_patch_apply_and_test(response, config, FIXTURES)


def run_one(
    bundle: ModelTaskBundle,
    path: EnumPath,
    onex: Path,
    api_key_file: Path | None,
    timeout_s: int,
    base_url: str | None = None,
    transcript_dir: Path | None = None,
) -> ModelRunRecord:
    if path is EnumPath.LOCAL:
        got = invoke_local(bundle, onex, timeout_s)
    else:
        got = invoke_cloud(bundle, onex, api_key_file, timeout_s, base_url)

    _, leak_fraction = split_answer(got.response)

    # The raw response is written beside the results, not merely hashed. A score
    # nobody can re-derive from the text that produced it is an assertion, not
    # evidence, and the rungs that execute code are exactly the ones a reader
    # will want to check by hand.
    if transcript_dir is not None:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        (transcript_dir / f"{bundle.task_id}.txt").write_text(
            got.response, encoding="utf-8"
        )

    if got.refused:
        verdict = ModelScore(
            outcome=EnumOutcome.REFUSED,
            score=0.0,
            mechanical=True,
            detail=got.notes,
        )
    else:
        verdict = score(bundle, got.response)

    classification = classify(
        bundle.prompt, bundle.scorer.value, bundle.dependent_reasoning_steps
    )

    return ModelRunRecord(
        task_id=bundle.task_id,
        rung=bundle.rung,
        path=path,
        delegation_id=got.delegation_id,
        tier=got.tier,
        backend_id=got.backend_id,
        model_id=got.model_id,
        input_tokens=got.input_tokens,
        output_tokens=got.output_tokens,
        total_tokens=got.total_tokens,
        cost_usd=got.cost_usd,
        model_latency_ms=got.model_latency_ms,
        wall_ms=got.wall_ms,
        quality_gate_passed=got.quality_gate_passed,
        quality_score=got.quality_score,
        leak_fraction=leak_fraction,
        response_chars=len(got.response),
        score=verdict,
        response_sha256=sha256_of(got.response),
        derived_rung=classification.rung,
        complexity_score=classification.score,
        complexity_features=json.loads(classification.features.model_dump_json()),
        notes=got.notes,
    )


def load_bundles(only: list[str] | None) -> list[ModelTaskBundle]:
    bundles = [
        ModelTaskBundle.model_validate_json(p.read_text(encoding="utf-8"))
        for p in sorted(BUNDLES.glob("*.json"))
    ]
    if only:
        wanted = {token.upper() for token in only}
        bundles = [
            b
            for b in bundles
            if b.task_id.upper() in wanted or b.rung.value.upper() in wanted
        ]
    return bundles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, choices=[p.value for p in EnumPath])
    parser.add_argument("--only", nargs="*", help="Task ids or rung ids to run.")
    parser.add_argument("--api-key-file", type=Path, default=None)
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "Gateway origin for the cloud path. Passed straight through, so a run "
            "records which gateway answered instead of inheriting a stored login."
        ),
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=(
            "Tasks in flight at once. Above 1 the wall_ms figures become upper "
            "bounds, because the runs contend; model_latency_ms from the receipt "
            "is the comparable number and the report must say which it used."
        ),
    )
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    omni_home = os.environ.get("OMNI_HOME")
    if not omni_home:
        print("OMNI_HOME must be set; there is no default.", file=sys.stderr)
        return 2
    onex = Path(omni_home) / "omnibase_infra" / "scripts" / "onex"
    if not onex.is_file():
        print(f"delegate CLI not found at {onex}", file=sys.stderr)
        return 2

    path = EnumPath(args.path)
    bundles = load_bundles(args.only)
    if not bundles:
        print("no bundles selected", file=sys.stderr)
        return 2

    transcripts = RESULTS / "responses" / path.value
    print(f"{len(bundles)} tasks, path={path.value}, concurrency={args.concurrency}")
    records: list[ModelRunRecord] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                run_one,
                bundle,
                path,
                onex,
                args.api_key_file,
                args.timeout,
                args.base_url,
                transcripts,
            ): bundle
            for bundle in bundles
        }
        for future in concurrent.futures.as_completed(futures):
            bundle = futures[future]
            try:
                record = future.result()
            except Exception as exc:
                record = ModelRunRecord(
                    task_id=bundle.task_id,
                    rung=bundle.rung,
                    path=path,
                    delegation_id=None,
                    tier=None,
                    backend_id=None,
                    model_id=None,
                    input_tokens=None,
                    output_tokens=None,
                    total_tokens=None,
                    cost_usd=None,
                    model_latency_ms=None,
                    wall_ms=0,
                    quality_gate_passed=None,
                    quality_score=None,
                    leak_fraction=0.0,
                    response_chars=0,
                    score=ModelScore(
                        outcome=EnumOutcome.HARNESS_ERROR,
                        score=0.0,
                        mechanical=True,
                        detail=f"{type(exc).__name__}: {exc}",
                    ),
                    response_sha256=sha256_of(""),
                    notes="harness failure, not a model result",
                )
            records.append(record)
            print(
                f"  {record.task_id:<7} {record.score.outcome.value:<13} "
                f"score={record.score.score:.2f} leak={record.leak_fraction:.2f} "
                f"{record.wall_ms / 1000:6.1f}s  {record.score.detail[:90]}"
            )

    records.sort(key=lambda r: r.task_id)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = args.out or (
        RESULTS / f"{path.value}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    )
    out.write_text(
        json.dumps(
            {
                "path": path.value,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "concurrency": args.concurrency,
                "base_url": args.base_url,
                "transcripts": str(transcripts.relative_to(HERE)),
                "paid_escalation_disabled": path is EnumPath.LOCAL,
                "records": [json.loads(r.model_dump_json()) for r in records],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    by_rung: dict[str, list[ModelRunRecord]] = {}
    for record in records:
        by_rung.setdefault(record.rung.value, []).append(record)
    print(f"\nwrote {out}\n")
    print(f"{'rung':<6}{'pass':>6}{'of':>4}{'rate':>8}{'leak':>8}")
    for rung in sorted(by_rung):
        group = by_rung[rung]
        passed = sum(1 for r in group if r.score.outcome is EnumOutcome.PASS)
        leaked = sum(1 for r in group if r.leak_fraction > 0.0)
        print(
            f"{rung:<6}{passed:>6}{len(group):>4}{passed / len(group):>8.2f}"
            f"{leaked / len(group):>8.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
