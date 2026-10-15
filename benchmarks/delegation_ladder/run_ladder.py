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
    uv run python benchmarks/delegation_ladder/run_ladder.py --backend local-coder --set tiered --only <TASK_ID> --out /tmp/x.jsonl

The rung benchmark (OMN-20844) runs the whole task set against one *pinned*
delegation rung at a time, so the ladder cannot move a task to another model
and a rung's share is that rung's own::

    python -m benchmarks.delegation_ladder.run_ladder --rung glm-5.3 --out-dir D
    python -m benchmarks.delegation_ladder.run_ladder --rung glm-5.3 --only R1-01 --out-dir D
    python -m benchmarks.delegation_ladder.run_ladder --rescore D
    python -m benchmarks.delegation_ladder.run_ladder --rescore D --only R1-01
    python -m benchmarks.delegation_ladder.run_ladder --table D [D ...]

The second line is the one-cell rerun. ``--shard i/N`` splits a rung's tasks
across hosts; ``--rescore`` re-derives every scorer verdict offline from the
saved responses (with ``--only``, just those tasks' cells), so a cell can be
checked without spending a model call.
"""

from __future__ import annotations

import argparse
import ast
import concurrent.futures
import gc
import importlib
import io
import json
import multiprocessing
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol

HERE = Path(__file__).resolve().parent
if str(HERE.parent.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent.parent))

from benchmarks.delegation_ladder.complexity import classify  # noqa: E402
from benchmarks.delegation_ladder.models import (  # noqa: E402
    EnumDifficulty,
    EnumOutcome,
    EnumPath,
    EnumScorerKind,
    ModelRunRecord,
    ModelScore,
    ModelTaskBundle,
    is_accepted,
)
from benchmarks.delegation_ladder.scorers import (  # noqa: E402
    TestRunner,
    score_diagnosis_location,
    score_exact_match,
    score_grounding_coverage,
    score_grounding_rubric,
    score_literal_equals,
    score_multi_file_edit_and_test,
    score_patch_apply_and_test,
    score_unit_test_execution,
    sha256_of,
    split_answer,
)

BUNDLES = HERE / "bundles"
# The structural hard corpus (build_bundles.py hard); its ids start H.
HARD_BUNDLES = HERE / "bundles_hard"
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
    attempts: list[dict[str, object]] = field(default_factory=list)
    escalation_count: int | None = None
    host: str | None = None


@dataclass(frozen=True)
class BackendSpec:
    """The invocation surface and endpoint label of one delegation rung."""

    kind: Literal["onex_delegate", "harness"]
    host_label: str
    backend_id: str | None = None
    model: str | None = None
    overlay: str | None = None


BACKENDS = {
    "local-coder": BackendSpec(
        "onex_delegate", "qwen-host-a", backend_id="local-coder"
    ),
    "local-omnipc2-chat": BackendSpec(
        "onex_delegate",
        "qwen-host-b",
        backend_id="local-coder",
        overlay=str(Path.home() / ".omninode/delegation/bifrost_overrides.pc2.yaml"),
    ),
    "openrouter-nemotron-ultra": BackendSpec(
        "onex_delegate", "openrouter-free", backend_id="openrouter-nemotron-ultra"
    ),
    "openrouter-nemotron-super": BackendSpec(
        "onex_delegate", "openrouter-free", backend_id="openrouter-nemotron-super"
    ),
    "glm-5.3-flash": BackendSpec("harness", "claude-glm", model="glm-5.3-flash"),
    "glm-5.3": BackendSpec("harness", "claude-glm", model="glm-5.3"),
    "unpinned": BackendSpec("onex_delegate", ""),
}


class SubprocessRun(Protocol):
    """Only the subprocess options used by the pinned driver."""

    def __call__(
        self,
        args: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: int,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]: ...


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def parse_deployed_receipt(receipt: dict[str, object]) -> Invocation:
    """Keep terminal failures as empty answers so the scorer counts them."""
    result = _mapping(receipt.get("result"))
    terminal = _mapping(result.get("terminal_payload"))
    # Deployed-lane receipts nest the payload one level down; in-process ones do not.
    payload = _mapping(terminal.get("payload")) or terminal or result
    metrics = _mapping(payload.get("metrics"))
    raw_attempts = payload.get("attempts")
    attempts = (
        [_mapping(a) for a in raw_attempts if isinstance(a, dict)]
        if isinstance(raw_attempts, list)
        else []
    )
    answering = next(
        (a for a in reversed(attempts) if a.get("acceptance_decision") == "accept"), {}
    )
    fields = (
        "backend_id",
        "tier",
        "model_id",
        "acceptance_decision",
        "acceptance_reason",
        "failure_class",
        "quality_gate_passed",
        "error_message",
    )
    kept = [{key: a.get(key) for key in fields} for a in attempts]
    for attempt in kept:
        error = _as_str(attempt.get("error_message"))
        attempt["error_message"] = error[:200] if error is not None else None
    status = _as_str(payload.get("status"))
    response = _as_str(payload.get("response")) or ""
    return Invocation(
        response=response if status == "completed" and response.strip() else "",
        delegation_id=_as_str(result.get("correlation_id")),
        tier=_as_str(answering.get("tier")),
        backend_id=_as_str(answering.get("backend_id")),
        model_id=_as_str(payload.get("model_name"))
        or _as_str(answering.get("model_id")),
        input_tokens=_as_int(metrics.get("input_tokens")),
        output_tokens=_as_int(metrics.get("output_tokens")),
        total_tokens=_as_int(metrics.get("total_tokens")),
        cost_usd=_as_float(metrics.get("cost_usd")),
        model_latency_ms=_as_int(metrics.get("latency_ms")),
        quality_gate_passed=_as_bool(payload.get("quality_gate_passed")),
        quality_score=_as_float(payload.get("quality_score")),
        wall_ms=0,
        refused=False,
        notes=f"status={status}; error_message={str(payload.get('error_message') or '')[:200]}",
        attempts=kept,
        escalation_count=_as_int(payload.get("escalation_count")),
        host=_as_str(answering.get("host")),
    )


def _no_answer(wall_ms: int, notes: str) -> Invocation:
    return replace(
        parse_deployed_receipt({}), wall_ms=wall_ms, refused=True, notes=notes
    )


def _stdout_receipt(stdout: str) -> dict[str, object] | None:
    for line in stdout.splitlines():
        if line.strip().startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


def invoke_pinned(
    bundle: ModelTaskBundle,
    spec: BackendSpec,
    *,
    onex: Path,
    timeout_s: int,
    run: SubprocessRun = subprocess.run,
    in_process_overlay: str | None = None,
) -> Invocation:
    """One invocation, with injectable plumbing and no delegation retries.

    ``in_process_overlay`` runs a deployed-lane rung in this process instead, on
    the bifrost overlay it names. The shared lane serves one consumer, so a
    benchmark that fans out through it times out on queueing, which says
    nothing about the rung.
    """
    env = dict(os.environ)
    env["ONEX_DELEGATION_ALLOW_PAID"] = "0"
    env.pop("ONEX_EVENT_BUS_TYPE", None)
    with tempfile.TemporaryDirectory(prefix=f"ladder-{bundle.task_id}-") as tmp:
        work = Path(tmp)
        if spec.kind == "harness":
            plugin_root = env.get("OMNI_PLUGIN_ROOT")
            if not plugin_root:
                raise RuntimeError("OMNI_PLUGIN_ROOT must be set for harness backends")
            if not spec.model:
                raise ValueError("harness backend must specify a model")
            prompt = work / "prompt.txt"
            prompt.write_text(bundle.prompt, encoding="utf-8")
            argv = [
                "python3",
                str(
                    Path(plugin_root)
                    / "skills/harness-delegate/scripts/harness_delegate.py"
                ),
                "run",
                "--harness",
                "claude-glm",
                "--model",
                spec.model,
                "--task-class",
                bundle.task_type,
                "--prompt-file",
                str(prompt),
                "--check",
                'test -s "$HARNESS_ANSWER_FILE"',
                "--lane",
                env.get("ONEX_LANE") or "ladder",
                "--ticket",
                "OMN-20844",
                "--out-dir",
                str(work),
            ]
        else:
            registry_root = env.get("OMNI_HOME")
            if not registry_root:
                raise RuntimeError("OMNI_HOME must be set; there is no default")
            env["PATH"] = (
                f"{Path(registry_root) / 'omnibase_infra/.venv/bin'}:{env.get('PATH', '')}"
            )
            argv = [
                "bash",
                str(onex),
                "delegate",
                bundle.prompt,
                "--task-type",
                bundle.task_type,
                "--ticket",
                "OMN-20844",
            ]
            overlay = spec.overlay or in_process_overlay
            if overlay:
                # LADDER_202_OVERLAY lets a host whose shipped overlay lacks the served
                # model name point at a per-run copy that has it.
                env["BIFROST_OVERLAY_PATH"] = (
                    env.get("LADDER_202_OVERLAY", overlay) if spec.overlay else overlay
                )
                argv += [
                    "--bus",
                    "inmemory",
                    "--locus",
                    "in-process",
                    "--state-root",
                    str(Path(registry_root) / ".onex_state"),
                ]
            else:
                argv += [
                    "--bus",
                    "kafka",
                    "--lane",
                    "dev",
                    "--locus",
                    "deployed-lane",
                    "--state-root",
                    str(Path(registry_root) / ".onex_state"),
                ]
            argv += ["--timeout", str(timeout_s), "--json"]
            if spec.backend_id:
                argv += ["--backend-id", spec.backend_id]

        started = time.monotonic()
        try:
            completed = run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_s + 180,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            tail = (
                stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
            )
            return _no_answer(
                int((time.monotonic() - started) * 1000),
                f"client exceeded {timeout_s + 180}s; {tail[-400:]}",
            )
        wall_ms = int((time.monotonic() - started) * 1000)
        if spec.kind == "onex_delegate":
            receipt = _stdout_receipt(completed.stdout)
            if receipt is not None:
                return replace(parse_deployed_receipt(receipt), wall_ms=wall_ms)
            return _no_answer(
                wall_ms,
                f"no receipt; rc={completed.returncode}; {completed.stderr[-400:]}",
            )

        receipts = list(work.glob("*/receipt.json"))
        if len(receipts) != 1:
            raise RuntimeError(
                f"harness_error: expected one receipt; rc={completed.returncode}; {completed.stderr[-400:]}"
            )
        harness = _mapping(json.loads(receipts[0].read_text(encoding="utf-8")))
        usage = _mapping(harness.get("usage"))
        answer_file = _as_str(harness.get("answer_file"))
        response = Path(answer_file).read_text(encoding="utf-8") if answer_file else ""
        duration = _as_float(harness.get("duration_s"))
        refused = bool(harness.get("timed_out")) or completed.returncode != 0
        return replace(
            parse_deployed_receipt({}),
            response=response,
            wall_ms=wall_ms,
            refused=refused,
            model_id=spec.model,
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            model_latency_ms=int(duration * 1000) if duration is not None else None,
            notes=f"outcome={harness.get('outcome')}; timed_out={harness.get('timed_out')}; error={harness.get('error')}",
        )


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
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        raise RuntimeError("OMNI_HOME must be set; there is no default (rule 8)")
    env = dict(os.environ)
    env["OMNI_HOME"] = registry_root
    env["ONEX_DELEGATION_ALLOW_PAID"] = "0"
    env.pop("ONEX_EVENT_BUS_TYPE", None)
    env["PATH"] = (
        f"{Path(registry_root) / 'omnibase_infra' / '.venv' / 'bin'}:{env['PATH']}"
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
                # NOT assumed. The recovered text is whatever the LAST rejected
                # attempt produced, and when the local tier's candidate is
                # refused the orchestrator tries the next tier and that
                # candidate can be refused too -- so the text may not be the
                # local model's at all. The tier is parsed from the same log
                # line; the backend is not printed there, so it is left absent
                # rather than filled in with a plausible-looking name.
                backend_id=None,
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
                    f"disabled; text recovered from this run's capture log, from the "
                    f"last refused candidate, produced at tier={tier}. reason={reason}"
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
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        raise RuntimeError("OMNI_HOME must be set; there is no default (rule 8)")
    env = dict(os.environ)
    env["PATH"] = (
        f"{Path(registry_root) / 'omnibase_infra' / '.venv' / 'bin'}:{env['PATH']}"
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
# Pinned delegation rungs (OMN-20844)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelRung:
    """One delegation rung the benchmark can pin a whole run to.

    ``surface`` is how the rung is reached: ``onex`` is ``onex delegate
    --backend-id <pin>`` on the deployed lane; ``harness`` is the Claude Code
    GLM harness, because the direct GLM backends are parked in the routing
    contract and the Coding Plan terms allow its quota only inside a supported
    tool. ``in_default_set`` is False for a rung with a hard free-tier cap.
    """

    name: str
    surface: str
    pin: str
    in_default_set: bool = True
    note: str = ""


RUNGS: dict[str, ModelRung] = {
    rung.name: rung
    for rung in (
        ModelRung("local-qwen3.8-27b", "onex", "local-coder"),
        ModelRung("glm-5.3-flash", "harness", "glm-5.3-flash"),
        ModelRung("glm-5.3", "harness", "glm-5.3"),
        ModelRung("openrouter-nemotron-ultra", "onex", "openrouter-nemotron-ultra"),
        ModelRung("openrouter-nemotron-super", "onex", "openrouter-nemotron-super"),
        ModelRung(
            "unpinned-ladder",
            "onex",
            "",
            in_default_set=False,
            note="no backend pin: the real ladder climbs as routing decides",
        ),
        ModelRung(
            "gemini-2.5-flash-lite",
            "onex",
            "cloud-gemini-flash",
            in_default_set=False,
            note="free tier: 20 requests per model per day",
        ),
        ModelRung(
            "gemini-2.5-flash",
            "onex",
            "cloud-gemini-2-5-flash",
            in_default_set=False,
            note="free tier: 20 requests per model per day",
        ),
    )
}

DEPLOYED_LANE_FLAGS: tuple[str, ...] = (
    "--bus",
    "kafka",
    "--lane",
    "dev",
    "--locus",
    "deployed-lane",
)
IN_PROCESS_FLAGS: tuple[str, ...] = ("--bus", "inmemory", "--locus", "in-process")


def rung_command(
    rung: ModelRung,
    *,
    prompt_file: Path,
    task_type: str,
    ticket: str,
    lane: str,
    state_root: Path,
    onex: Path,
    harness_script: Path,
    bus_flags: tuple[str, ...] = DEPLOYED_LANE_FLAGS,
    harness_out_dir: Path | None = None,
) -> list[str]:
    """The argv for one task on one pinned rung. Nothing here retries or falls back."""
    if rung.surface == "onex":
        return [
            "bash",
            str(onex),
            "delegate",
            "--prompt-file",
            str(prompt_file),
            "--task-type",
            task_type,
            # An empty pin is the unpinned ladder: no flag, so routing decides.
            *(["--backend-id", rung.pin] if rung.pin else []),
            "--ticket",
            ticket,
            *bus_flags,
            "--state-root",
            str(state_root),
            "--json",
        ]
    return [
        "python3",
        str(harness_script),
        "run",
        "--harness",
        "claude-glm",
        "--model",
        rung.pin,
        "--task-class",
        task_type,
        "--prompt-file",
        str(prompt_file),
        "--check",
        'test -s "$HARNESS_ANSWER_FILE"',
        "--lane",
        lane,
        "--ticket",
        ticket,
        "--out-dir",
        str(harness_out_dir or state_root / "harness-delegate"),
    ]


def parse_onex_stdout(
    stdout: str, state_root: Path, stderr: str = ""
) -> dict[str, Any]:
    """Fields of one ``onex delegate --json`` run, or a ``failure_kind``."""
    rec: dict[str, Any] = {"response": ""}
    line = next(
        (ln for ln in reversed(stdout.splitlines()) if ln.strip().startswith("{")),
        None,
    )
    if line is None:
        rec["failure_kind"] = "no_receipt"
        rec["detail"] = (stderr.strip() or stdout.strip())[-300:]
        return rec
    doc = json.loads(line)
    rec["run_id"] = doc.get("run_id")
    rec["receipt"] = f"{state_root}/runs/{doc.get('run_id')}/receipt.json"
    payload = ((doc.get("result") or {}).get("terminal_payload") or {}).get(
        "payload"
    ) or {}
    if not payload:
        rec["failure_kind"] = "no_terminal_payload"
        rec["detail"] = json.dumps(doc.get("result"))[:300]
        return rec
    metrics = payload.get("metrics") or {}
    attempts = payload.get("attempts") or []
    last = attempts[-1] if attempts else {}
    rec.update(
        status=payload.get("status"),
        provider=payload.get("provider"),
        model_name=payload.get("model_name"),
        backend_id_answered=last.get("backend_id"),
        gate_passed=payload.get("quality_gate_passed"),
        quality_score=payload.get("quality_score"),
        acceptance=last.get("acceptance_decision"),
        acceptance_reason=last.get("acceptance_reason"),
        gates_failed=payload.get("quality_gates_failed"),
        input_tokens=metrics.get("input_tokens"),
        output_tokens=metrics.get("output_tokens"),
        cost_usd=metrics.get("cost_usd"),
        model_latency_ms=metrics.get("latency_ms"),
        escalation_count=payload.get("escalation_count"),
        ladder=ladder_trace([a for a in attempts if isinstance(a, dict)]),
        response=payload.get("response") or "",
    )
    if (
        payload.get("status") != "completed"
        or last.get("acceptance_decision") != "accept"
    ):
        rec["failure_kind"] = (
            last.get("failure_class")
            or (
                f"gate_reject:{last.get('acceptance_reason')}"
                if last.get("acceptance_decision") == "reject"
                else None
            )
            or f"status_{payload.get('status')}"
        )
        rec["detail"] = (payload.get("error_message") or "")[:300]
    return rec


def ladder_trace(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    """What an unpinned ladder run did: who answered, how far it climbed, why each rung was refused.

    ``attempts`` is the receipt's own list, one entry per rung tried. The
    answering rung is the one whose decision was ``accept``; a run that never
    accepted names none. ``escalations`` is the number of rungs tried after the
    first, so a task the first rung answered took none.
    """
    accepted = next(
        (a for a in attempts if a.get("acceptance_decision") == "accept"), None
    )
    refused = [
        {
            "backend_id": a.get("backend_id"),
            "tier": a.get("tier"),
            "decision": a.get("acceptance_decision"),
            "reason": a.get("acceptance_reason"),
            "error": a.get("error_message"),
        }
        for a in attempts
        if a is not accepted
    ]
    return {
        "answered_by": accepted.get("backend_id") if accepted else None,
        "answered_tier": accepted.get("tier") if accepted else None,
        "escalations": max(len(attempts) - 1, 0),
        "refused": refused,
    }


def parse_harness_stdout(stdout: str, stderr: str) -> dict[str, Any]:
    """Fields of one GLM harness run, read from the receipt it names."""
    rec: dict[str, Any] = {"response": ""}
    receipt_path = None
    for token in stdout.split():
        if token.startswith("receipt="):
            receipt_path = token[len("receipt=") :]
    if not receipt_path or not Path(receipt_path).is_file():
        rec["failure_kind"] = "no_receipt"
        rec["detail"] = (stderr or stdout).strip()[-300:]
        return rec
    receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    usage = receipt.get("usage") or {}
    rec.update(
        run_id=receipt.get("run_id"),
        receipt=receipt_path,
        status=receipt.get("outcome"),
        provider="glm-coding-plan-harness",
        model_name=receipt.get("model"),
        backend_id_answered=receipt.get("backend_id"),
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cost_usd=None,
        model_latency_ms=None,
        harness_duration_s=receipt.get("duration_s"),
    )
    answer_file = receipt.get("answer_file")
    if answer_file and Path(answer_file).is_file():
        rec["response"] = Path(answer_file).read_text(encoding="utf-8")
    if receipt.get("outcome") != "done" or not rec["response"].strip():
        rec["failure_kind"] = (
            "timeout"
            if receipt.get("timed_out")
            else (
                str(receipt.get("error"))[:80]
                if receipt.get("error")
                else f"outcome_{receipt.get('outcome')}"
            )
        )
        rec["detail"] = str(receipt.get("error"))[:300]
    return rec


def read_receipt_facts(receipt_path: str | None) -> dict[str, Any]:
    """What the run's own receipt says about where it was answered, if readable.

    ``endpoint`` names the host that served the model; ``backend_pin_honoured``
    says whether the pinned rung answered or the ladder moved the task, which
    would make the row another rung's result under this rung's name.
    """
    if not receipt_path or not Path(receipt_path).is_file():
        return {}
    try:
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        key: receipt[key]
        for key in ("endpoint", "backend_pin_honoured", "model")
        if key in receipt
    }


def run_on_rung(
    bundle: ModelTaskBundle,
    rung: ModelRung,
    *,
    work_dir: Path,
    transcript_dir: Path,
    ticket: str,
    lane: str,
    state_root: Path,
    onex: Path,
    harness_script: Path,
    bus_flags: tuple[str, ...],
    timeout_s: int,
) -> dict[str, Any]:
    """One task, one pinned rung, one attempt. Never retried."""
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = work_dir / f"{bundle.task_id}.txt"
    prompt_file.write_text(bundle.prompt, encoding="utf-8")
    argv = rung_command(
        rung,
        prompt_file=prompt_file,
        task_type=bundle.task_type,
        ticket=ticket,
        lane=lane,
        state_root=state_root,
        onex=onex,
        harness_script=harness_script,
        bus_flags=bus_flags,
    )
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
        wall_ms = int((time.monotonic() - started) * 1000)
        got = (
            parse_onex_stdout(completed.stdout, state_root, completed.stderr)
            if rung.surface == "onex"
            else parse_harness_stdout(completed.stdout, completed.stderr)
        )
        got["rc"] = completed.returncode
    except subprocess.TimeoutExpired:
        wall_ms = int((time.monotonic() - started) * 1000)
        got = {"response": "", "failure_kind": f"client_timeout_{timeout_s}s"}

    response = got.pop("response", "") or ""
    if rung.surface == "onex":
        facts = read_receipt_facts(got.get("receipt"))
        got.update(
            receipt_endpoint=facts.get("endpoint"),
            backend_pin_honoured=facts.get("backend_pin_honoured"),
        )
    transcript_dir.mkdir(parents=True, exist_ok=True)
    (transcript_dir / f"{bundle.task_id}.json").write_text(
        json.dumps(
            {
                "task_id": bundle.task_id,
                "model_rung": rung.name,
                "sha256": sha256_of(response),
                "response": response,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    if got.get("failure_kind") and not response.strip():
        outcome, point, detail = "no_answer", 0.0, str(got.get("detail", ""))
    else:
        verdict = score(bundle, response)
        outcome, point, detail = verdict.outcome.value, verdict.score, verdict.detail
    delivered = not got.get("failure_kind")
    scored_pass = outcome == EnumOutcome.PASS.value
    record: dict[str, Any] = {
        "model_rung": rung.name,
        "surface": rung.surface,
        "pin": rung.pin,
        "task_id": bundle.task_id,
        "level": bundle.rung.value,
        "task_type": bundle.task_type,
        "difficulty": _difficulty(bundle),
        "scorer": bundle.scorer.value,
        "accepted": bool(delivered and scored_pass),
        "delivered": delivered,
        "scorer_outcome": outcome,
        "score": point,
        "scorer_detail": detail[:300],
        "response_sha256": sha256_of(response),
        "response_chars": len(response),
        "wall_ms": wall_ms,
        "host": PUBLISHED_HOST,
        "driver_sha": _driver_sha(),
        **got,
    }
    for private in PRIVATE_RECORD_FIELDS:
        record.pop(private, None)
    for text_field in ("scorer_detail", "detail"):
        if isinstance(record.get(text_field), str):
            record[text_field] = publishable_text(record[text_field])
    if not delivered:
        record["failure_kind"] = got.get("failure_kind")
    elif not scored_pass:
        record["failure_kind"] = f"scorer_{outcome}"
    return record


# A record is committed to a public repository, so it names neither the machine
# that ran it nor the internal runs behind it. The command line, the run and
# delegation ids and the receipt path identify internal systems and add nothing a
# reader can reproduce from; the host is a fixed label.
PUBLISHED_HOST = "lab-host"
PRIVATE_RECORD_FIELDS = ("command", "run_id", "receipt", "delegation_id")
_PRIVATE_TEXT: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$?\{?OMNI_HOME\}?(?:/[^\s\"',)]*)?"), "<registry>"),
    (re.compile(r"[\w.-]*\.onex_state[^\s\"',)]*"), "<state>"),
    (re.compile(r"/(?:home|Users|data|scratch|srv|mnt)/[^\s\"',)]+"), "<path>"),
    (re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.ts\.net\b"), "<host>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<address>"),
    (
        re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
        "<id>",
    ),
)


def publishable_text(text: str) -> str:
    """``text`` with paths, addresses, hostnames and run ids replaced."""
    for pattern, placeholder in _PRIVATE_TEXT:
        text = pattern.sub(placeholder, text)
    return text


def _difficulty(bundle: ModelTaskBundle) -> str | None:
    from benchmarks.delegation_ladder.summarise_results import difficulty_tier

    return difficulty_tier(bundle.task_type, len(bundle.prompt))


def _driver_sha() -> str | None:
    """The commit of the checkout the driver ran from, so a cell names its code."""
    done = subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or None if done.returncode == 0 else None


def _rescore_cell(
    bundle: ModelTaskBundle,
    saved: Path,
    recorded: dict[str, Any],
    *,
    test_runner: TestRunner | None = None,
) -> tuple[str, float, str]:
    """``(outcome, score, detail)`` re-derived from a cell's saved response."""
    blob = json.loads(saved.read_text(encoding="utf-8"))
    response = blob["response"]
    digest = sha256_of(response)
    if digest != blob["sha256"] or digest != recorded["response_sha256"]:
        raise ValueError(
            f"{bundle.task_id}: saved response sha does not match the record "
            f"(file {blob['sha256'][:12]}, bytes {digest[:12]}, "
            f"record {recorded['response_sha256'][:12]})"
        )
    if not response.strip() and recorded.get("scorer_outcome") == "no_answer":
        return "no_answer", 0.0, str(recorded.get("scorer_detail", ""))
    verdict = score(bundle, response, test_runner=test_runner)
    return verdict.outcome.value, verdict.score, verdict.detail


def rescore_record(
    bundle: ModelTaskBundle, saved: Path, recorded: dict[str, Any]
) -> str:
    """Re-derive one cell's scorer verdict from its saved response, offline.

    Refuses when the saved bytes are not the bytes the record was scored from:
    a verdict re-derived from different text reproduces nothing.
    """
    return _rescore_cell(bundle, saved, recorded)[0]


def rescored_record(
    recorded: dict[str, Any], outcome: str, point: float, detail: str
) -> dict[str, Any]:
    """The record with a re-derived verdict, the very first verdict kept beside it.

    Acceptance stays what it is made of: delivered past the gate AND passed by
    the scorer. A rescored pass the gate refused is still not accepted.
    """
    if outcome == recorded["scorer_outcome"]:
        return dict(recorded)
    record = dict(recorded)
    record.setdefault("first_scorer_outcome", recorded["scorer_outcome"])
    record.setdefault("first_scorer_detail", recorded.get("scorer_detail"))
    record["scorer_outcome"] = outcome
    record["score"] = point
    record["scorer_detail"] = detail[:300]
    record["accepted"] = bool(record["delivered"] and outcome == EnumOutcome.PASS.value)
    if record["delivered"]:
        if outcome == EnumOutcome.PASS.value:
            record.pop("failure_kind", None)
        else:
            record["failure_kind"] = f"scorer_{outcome}"
    return record


# Failures that say the plumbing broke rather than what the rung answered: no
# receipt came back, the lane's own wait expired, or the client gave up. A rung
# that is merely slow reproduces them on a lone rerun; a contended lane does not.
_INFRA_FAILURES = ("no_receipt", "no_terminal_payload", "client_timeout")


# The runtime was redeployed or restarted under a delegation in flight. Its
# terminal is ``status_failed`` like a model failure's, so the sentence names it.
_RUNTIME_SHUTDOWN = "the runtime running this delegation shut down"


def is_infra_failure(record: dict[str, Any]) -> bool:
    kind = str(record.get("failure_kind") or "")
    if record.get("scorer_outcome") != "no_answer":
        return False
    return kind.startswith(_INFRA_FAILURES) or (
        kind == "status_failed" and _RUNTIME_SHUTDOWN in str(record.get("detail") or "")
    )


def partition_infra_failures(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a rung's records into those that stand and those to run once more."""
    stand = [r for r in records if not is_infra_failure(r)]
    redo = [r for r in records if is_infra_failure(r)]
    return stand, redo


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _results_files(dirs: list[Path]) -> list[Path]:
    files: list[Path] = []
    for directory in dirs:
        files += (
            sorted(directory.glob("*.jsonl")) if directory.is_dir() else [directory]
        )
    return files


def _warm_rescore_worker(bundles: list[ModelTaskBundle]) -> None:
    """Load dependencies once, in a spawned worker that never runs a subject.

    Each probe forks this clean worker, so imports and mutations made by a
    generated test cannot reach another probe. Preparing pytest in an empty
    directory preserves the isolated runner's plugin set and assertion rewriting.
    """
    import pytest

    original = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="ladder-preload-") as tmp:
        try:
            os.chdir(tmp)
            Path("test_preload.py").write_text("", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as output, redirect_stderr(output):
                rc = pytest.main(
                    [
                        "test_preload.py",
                        "--collect-only",
                        "-q",
                        "-p",
                        "no:cacheprovider",
                    ]
                )
            if rc not in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
                raise RuntimeError(f"pytest preload failed: {output.getvalue()}")
        finally:
            os.chdir(original)

    modules: set[str] = set()
    for bundle in bundles:
        for key, text in bundle.scorer_config.items():
            if not key.endswith("_text") or not isinstance(text, str):
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    modules.add(node.module)
                elif isinstance(node, ast.Import):
                    modules.update(alias.name for alias in node.names)
    for module in sorted(modules):
        if module.startswith(("omnibase_core.", "omnibase_infra.", "omnimarket.")):
            try:
                importlib.import_module(module)
            except ImportError:
                # A missing dependency must still fail in the probe, where the
                # scorer can report it; warming is not an installation step.
                continue

    # Keep the preloaded graph out of subsequent collections and copy-on-write
    # churn. New objects created by a probe remain subject to normal collection.
    gc.freeze()


def _run_forked_pytest(
    workdir: Path, test_name: str, timeout_s: int
) -> tuple[int, str]:
    """Run the same pytest arguments and timeout in a fresh, preloaded child."""
    import pytest

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output:
        sys.stdout.flush()
        sys.stderr.flush()
        pid = os.fork()
        if pid == 0:
            rc = 3
            try:
                os.dup2(output.fileno(), 1)
                os.dup2(output.fileno(), 2)
                os.chdir(workdir)
                sys.dont_write_bytecode = True
                os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
                rc = int(pytest.main([test_name, "-q", "-p", "no:cacheprovider"]))
            except BaseException:
                traceback.print_exc()
            finally:
                sys.stdout.flush()
                sys.stderr.flush()
                os._exit(rc)

        deadline = time.monotonic() + timeout_s
        try:
            while True:
                done, status = os.waitpid(pid, os.WNOHANG)
                if done:
                    break
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    return 124, f"timed out after {timeout_s}s"
                time.sleep(0.005)
        except BaseException:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
            raise
        output.seek(0)
        return os.waitstatus_to_exitcode(status), output.read().strip()[-1200:]


def _rescore_worker_group(
    cells: list[tuple[ModelTaskBundle, Path, dict[str, Any]]],
) -> list[tuple[str, float, str]]:
    """Score one task's rungs, reusing identical test/source executions only.

    In particular, each patch task's committed pre-fix control is identical on
    every rung. The cache lives for this group only; hashes and recorded verdicts
    are still checked independently for every cell, on every rescore invocation.
    """
    cache: dict[tuple[str, int, tuple[tuple[str, bytes], ...]], tuple[int, str]] = {}

    def run_test(workdir: Path, test_name: str, timeout_s: int) -> tuple[int, str]:
        sources = tuple(
            (path.name, path.read_bytes()) for path in sorted(workdir.glob("*.py"))
        )
        key = (test_name, timeout_s, sources)
        if key not in cache:
            cache[key] = _run_forked_pytest(workdir, test_name, timeout_s)
        return cache[key]

    return [
        _rescore_cell(bundle, saved, recorded, test_runner=run_test)
        for bundle, saved, recorded in cells
    ]


def _rescore_parallel_cells(
    cells: list[tuple[ModelTaskBundle, Path, dict[str, Any]]],
) -> list[tuple[str, float, str]]:
    """Preload once in a spawned coordinator, then share its clean import graph.

    The coordinator never executes a subject. Workers and their probe children
    inherit dependencies, but none inherit imports or mutations from a probe.
    """
    used = {bundle.task_id: bundle for bundle, _, _ in cells}
    _warm_rescore_worker(list(used.values()))
    groups: dict[str, list[tuple[ModelTaskBundle, Path, dict[str, Any]]]] = {}
    indices: dict[str, list[int]] = {}
    for index, cell in enumerate(cells):
        task_id = cell[0].task_id
        groups.setdefault(task_id, []).append(cell)
        indices.setdefault(task_id, []).append(index)
    with concurrent.futures.ProcessPoolExecutor(
        max_workers=min(4, len(groups), os.cpu_count() or 1),
        mp_context=multiprocessing.get_context("fork"),
    ) as pool:
        groups_scored = list(pool.map(_rescore_worker_group, groups.values()))
    ordered: dict[int, tuple[str, float, str]] = {}
    for task_id, verdicts in zip(groups, groups_scored, strict=True):
        for index, verdict in zip(indices[task_id], verdicts, strict=True):
            ordered[index] = verdict
    return [ordered[index] for index in range(len(cells))]


def rescore_dir(
    directory: Path, *, write: bool = False, only: list[str] | None = None
) -> int:
    """Rescore every record under ``directory``, or only the tasks ``only`` names.

    Without ``write`` it reports each cell whose verdict differs and exits
    non-zero if any does. With ``write`` it rewrites the records with the
    re-derived verdicts (the first verdict kept as ``first_scorer_outcome``) and
    exits zero. ``only`` cannot be combined with ``write``: a rewrite of a
    filtered file would drop the cells left out.
    """
    if write and only:
        raise ValueError("--rescore --write rewrites whole files; drop --only")
    wanted = {token.upper() for token in only} if only else None
    bundles = {b.task_id: b for b in load_bundles(None, include_hard=True)}
    files: dict[Path, list[dict[str, Any]]] = {}
    for path in _results_files([directory]):
        records = _read_jsonl(path)
        if wanted is not None:
            records = [r for r in records if str(r["task_id"]).upper() in wanted]
        files[path] = records
    cells = [
        (
            bundles[recorded["task_id"]],
            directory
            / "responses"
            / recorded["model_rung"]
            / f"{recorded['task_id']}.json",
            recorded,
        )
        for records in files.values()
        for recorded in records
    ]
    executing = {
        EnumScorerKind.UNIT_TEST_EXECUTION,
        EnumScorerKind.PATCH_APPLY_AND_TEST,
    }
    forked = [
        index
        for index, (bundle, _, _) in enumerate(cells)
        if hasattr(os, "fork") and bundle.scorer in executing
    ]
    threaded = sorted(set(range(len(cells))) - set(forked))
    scored: dict[int, tuple[str, float, str]] = {}
    if forked:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=1,
            mp_context=multiprocessing.get_context("spawn"),
        ) as coordinator:
            forked_verdicts = coordinator.submit(
                _rescore_parallel_cells, [cells[index] for index in forked]
            ).result()
        scored.update(zip(forked, forked_verdicts, strict=True))
    # Each other cell scores in its own temporary directory, so cells can run
    # at once; the hard tier's cells each run a repository test file twice.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        threaded_verdicts = list(
            pool.map(lambda index: _rescore_cell(*cells[index]), threaded)
        )
    scored.update(zip(threaded, threaded_verdicts, strict=True))
    verdicts = iter(scored[index] for index in range(len(cells)))
    checked = changed = 0
    for jsonl, records in files.items():
        updated: list[dict[str, Any]] = []
        for recorded in records:
            outcome, point, detail = next(verdicts)
            checked += 1
            if outcome != recorded["scorer_outcome"]:
                changed += 1
                print(
                    f"{'REWRITTEN' if write else 'MISMATCH'} {recorded['model_rung']} "
                    f"{recorded['task_id']}: recorded {recorded['scorer_outcome']}, "
                    f"rescored {outcome}"
                )
            updated.append(rescored_record(recorded, outcome, point, detail))
        if write:
            jsonl.write_text(
                "".join(json.dumps(r) + "\n" for r in updated), encoding="utf-8"
            )
    print(
        f"rescored {checked} cell(s), {changed} {'rewritten' if write else 'mismatch(es)'}"
    )
    return 0 if write else (1 if changed else 0)


def print_table(
    dirs: list[Path], *, by_difficulty: bool = False, by_feature: bool = False
) -> int:
    from benchmarks.delegation_ladder.summarise_results import (
        bundle_features,
        class_rung_table,
        class_separation,
        difficulty_tier,
        first_failing_rung_tier,
        render_difficulty_table,
        render_feature_report,
        render_table,
        tier_rung_table,
    )

    records: dict[tuple[str, str], dict[str, Any]] = {}
    for jsonl in _results_files(dirs):
        for rec in _read_jsonl(jsonl):
            records[(rec["model_rung"], rec["task_id"])] = rec
    if not records:
        print("no records", file=sys.stderr)
        return 2
    if by_difficulty:
        tiers = {
            b.task_id: tier
            for b in load_bundles(None)
            if (tier := difficulty_tier(b.task_type, len(b.prompt))) is not None
        }
        tier_rows = tier_rung_table(list(records.values()), tiers, list(RUNGS))
        print(render_difficulty_table(tier_rows, first_failing_rung_tier(tier_rows)))
        return 0
    if by_feature:
        hard = [
            (b, f)
            for b in load_bundles(None, include_hard=True)
            if (f := bundle_features(b.scorer_config)) is not None
        ]
        features = {b.task_id: f for b, f in hard}
        pinned = [name for name, rung in RUNGS.items() if rung.pin]
        print(
            render_feature_report(
                list(records.values()),
                features,
                {b.task_id: b.task_type for b, _ in hard},
                pinned,
            )
        )
        return 0
    rows = class_rung_table(list(records.values()), list(RUNGS))
    print(render_table(rows, class_separation(rows)))
    return 0


def run_rung_benchmark(args: argparse.Namespace) -> int:
    registry_root = os.environ.get("OMNI_HOME")
    if not registry_root:
        print("OMNI_HOME must be set; there is no default.", file=sys.stderr)
        return 2
    onex = Path(registry_root) / "omnibase_infra" / "scripts" / "onex"
    plugin_root = os.environ.get("OMNI_PLUGIN_ROOT") or os.environ.get(
        "CLAUDE_PLUGIN_ROOT"
    )
    harness_script = (
        Path(plugin_root)
        / "skills"
        / "harness-delegate"
        / "scripts"
        / "harness_delegate.py"
        if plugin_root
        else Path("harness_delegate.py")
    )
    rung = RUNGS[args.rung]
    if rung.surface == "harness" and not harness_script.is_file():
        print(
            "OMNI_PLUGIN_ROOT (the omni plugin root) must name the harness-delegate skill",
            file=sys.stderr,
        )
        return 2
    index, count = (int(x) for x in args.shard.split("/"))
    bundles = [b for i, b in enumerate(load_bundles(args.only)) if i % count == index]
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "" if count == 1 else f".shard-{index}of{count}"
    jsonl = out_dir / f"{rung.name}{suffix}.jsonl"
    existing = _read_jsonl(jsonl)
    if args.rerun_infra:
        # The first attempt of a plumbing failure is archived, not discarded;
        # the rerun is the one attempt that stands, and a rerun that fails the
        # same way is the result for that cell.
        stand, redo = partition_infra_failures(existing)
        if redo:
            archive = out_dir / "first-attempt"
            archive.mkdir(exist_ok=True)
            with (archive / jsonl.name).open("a", encoding="utf-8") as handle:
                for record in redo:
                    handle.write(json.dumps(record) + "\n")
            jsonl.write_text(
                "".join(json.dumps(r) + "\n" for r in stand), encoding="utf-8"
            )
        existing = stand
    done = {r["task_id"] for r in existing}
    todo = [b for b in bundles if b.task_id not in done]
    state_root = Path(args.state_root or Path(registry_root) / ".onex_state")
    bus_flags = IN_PROCESS_FLAGS if args.in_process else DEPLOYED_LANE_FLAGS
    print(
        f"{rung.name}: {len(todo)} to run, {len(done)} already recorded, "
        f"shard {args.shard}, concurrency {args.concurrency}, host {socket.gethostname()}",
        flush=True,
    )

    def one(bundle: ModelTaskBundle) -> dict[str, Any]:
        return run_on_rung(
            bundle,
            rung,
            work_dir=out_dir / "prompts",
            transcript_dir=out_dir / "responses" / rung.name,
            ticket=args.ticket,
            lane=args.lane,
            state_root=state_root,
            onex=onex,
            harness_script=harness_script,
            bus_flags=bus_flags,
            timeout_s=args.timeout,
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for record in pool.map(one, todo):
            with jsonl.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
            print(
                f"{rung.name:26} {record['task_id']:7} accepted={record['accepted']!s:5} "
                f"{record['scorer_outcome']:9} {record['wall_ms'] / 1000:6.1f}s "
                f"{record.get('failure_kind') or ''}",
                flush=True,
            )
    return 0


# --------------------------------------------------------------------------
# Scoring dispatch
# --------------------------------------------------------------------------


def score(
    bundle: ModelTaskBundle, response: str, *, test_runner: TestRunner | None = None
) -> ModelScore:
    config = dict(bundle.scorer_config)
    match bundle.scorer:
        case EnumScorerKind.GROUNDING_RUBRIC:
            return score_grounding_rubric(response, config)
        case EnumScorerKind.GROUNDING_COVERAGE:
            return score_grounding_coverage(response, config)
        case EnumScorerKind.EXACT_MATCH:
            return score_exact_match(response, config)
        case EnumScorerKind.UNIT_TEST_EXECUTION:
            return score_unit_test_execution(
                response, config, FIXTURES, test_runner=test_runner
            )
        case EnumScorerKind.DIAGNOSIS_LOCATION:
            return score_diagnosis_location(response, config)
        case EnumScorerKind.PATCH_APPLY_AND_TEST:
            return score_patch_apply_and_test(
                response, config, FIXTURES, test_runner=test_runner
            )
        case EnumScorerKind.MULTI_FILE_EDIT_AND_TEST:
            return score_multi_file_edit_and_test(response, config)
        case EnumScorerKind.LITERAL_EQUALS:
            return score_literal_equals(response, config)


def run_one(
    bundle: ModelTaskBundle,
    path: EnumPath,
    onex: Path,
    api_key_file: Path | None,
    timeout_s: int,
    base_url: str | None = None,
    transcript_dir: Path | None = None,
    *,
    backend: str | None = None,
    in_process_overlay: str | None = None,
) -> ModelRunRecord:
    if backend is not None:
        got = invoke_pinned(
            bundle,
            BACKENDS[backend],
            onex=onex,
            timeout_s=timeout_s,
            in_process_overlay=in_process_overlay,
        )
    elif path is EnumPath.LOCAL:
        got = invoke_local(bundle, onex, timeout_s)
    else:
        got = invoke_cloud(bundle, onex, api_key_file, timeout_s, base_url)

    _, leak_fraction = split_answer(got.response)

    # The raw response is written beside the results, not merely hashed. A score
    # nobody can re-derive from the text that produced it is an assertion, not
    # evidence, and the rungs that execute code are exactly the ones a reader
    # will want to check by hand.
    #
    # JSON rather than a plain text file, deliberately: the repository's
    # whitespace-fixing pre-commit hooks rewrite a committed .txt, which
    # silently breaks the sha the score was computed from. Inside a JSON string
    # the bytes survive, and the hash travels with them.
    if transcript_dir is not None:
        transcript_dir.mkdir(parents=True, exist_ok=True)
        (transcript_dir / f"{bundle.task_id}.json").write_text(
            json.dumps(
                {
                    "task_id": bundle.task_id,
                    "path": path.value,
                    "sha256": sha256_of(got.response),
                    "response": got.response,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
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

    # The routing rubric is a production contract and is not changed here. A
    # predicted-literal task is graded by comparison with a held answer, which is
    # what the rubric already calls exact_match.
    rubric_scorer = (
        EnumScorerKind.EXACT_MATCH
        if bundle.scorer is EnumScorerKind.LITERAL_EQUALS
        else bundle.scorer
    )
    classification = classify(
        bundle.prompt, rubric_scorer.value, bundle.dependent_reasoning_steps
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
        backend=backend,
        difficulty=bundle.difficulty,
        task_type=bundle.task_type,
        host=(got.host if backend == "unpinned" else BACKENDS[backend].host_label)
        if backend is not None
        else None,
        accepted=is_accepted(verdict.outcome, got.quality_gate_passed),
        attempts=got.attempts,
        escalation_count=got.escalation_count,
    )


def load_bundles(
    only: list[str] | None,
    task_set: str = "ladder",
    classes: list[str] | None = None,
    difficulty: EnumDifficulty | None = None,
    *,
    include_hard: bool = False,
) -> list[ModelTaskBundle]:
    """The chosen corpus; for the ladder set the hard corpus too when asked for or named by ``only``."""
    source = BUNDLES / "tiered" if task_set == "tiered" else BUNDLES
    paths = sorted(source.glob("*.json"))
    if task_set != "tiered" and (include_hard or only):
        paths += sorted(HARD_BUNDLES.glob("*.json"))
    bundles = [
        ModelTaskBundle.model_validate_json(p.read_text(encoding="utf-8"))
        for p in paths
    ]
    if only:
        wanted = {token.upper() for token in only}
        bundles = [
            b
            for b in bundles
            if b.task_id.upper() in wanted or b.rung.value.upper() in wanted
        ]
    return [
        b
        for b in bundles
        if (not classes or b.task_type in classes)
        and (difficulty is None or b.difficulty == difficulty)
    ]


def remaining_bundles(
    bundles: list[ModelTaskBundle],
    done_ids: set[str],
) -> list[ModelTaskBundle]:
    return [b for b in bundles if b.task_id not in done_ids]


def completed_task_ids(path: Path) -> set[str]:
    """The latest plumbing failure stays eligible for an explicit resume."""
    if not path.exists():
        return set()
    records: dict[str, dict[str, object]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = _mapping(json.loads(line))
            task_id = _as_str(record.get("task_id"))
            if task_id is not None:
                records[task_id] = record
    return {
        task_id
        for task_id, record in records.items()
        if "harness_error" not in str(record.get("notes", ""))
        and _mapping(record.get("score")).get("outcome") != "harness_error"
    }


def check_results_file(path: Path, *, resume: bool) -> None:
    if path.exists() and not resume:
        raise FileExistsError(f"results file already exists: {path}; use --resume")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    surface = parser.add_mutually_exclusive_group()
    surface.add_argument("--path", choices=["local", "cloud"])
    surface.add_argument("--backend", choices=list(BACKENDS))
    surface.add_argument(
        "--rung",
        choices=sorted(RUNGS),
        help="Run the task set against one pinned delegation rung (OMN-20844).",
    )
    parser.add_argument("--set", choices=["ladder", "tiered"], default="ladder")
    parser.add_argument("--classes", nargs="+")
    parser.add_argument("--difficulty", choices=[d.value for d in EnumDifficulty])
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--in-process-overlay",
        default=None,
        help="Run onex_delegate rungs in this process on this bifrost overlay "
        "(must carry the served model_name) instead of the shared dev lane.",
    )
    parser.add_argument(
        "--shard",
        default="0/1",
        help="K/N (also i/N for --rung): run only the tasks whose position modulo N is K, so one rung "
        "can be split across hosts.",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="With --rescore: write the re-derived verdicts into the records.",
    )
    parser.add_argument(
        "--by-difficulty",
        action="store_true",
        help="With --table: split the test and code_generation classes by tier.",
    )
    parser.add_argument(
        "--by-feature",
        action="store_true",
        help="With --table: failure and escalation by structural feature (hard corpus).",
    )
    parser.add_argument(
        "--rescore",
        type=Path,
        metavar="DIR",
        help="Re-derive every scorer verdict under DIR from its saved response.",
    )
    parser.add_argument(
        "--table",
        nargs="+",
        type=Path,
        metavar="DIR",
        help="Print the per-class, per-rung table over the results in DIR(s).",
    )
    parser.add_argument("--out-dir", type=Path, default=RESULTS / "rungs")
    parser.add_argument(
        "--rerun-infra",
        action="store_true",
        help="Run once more the cells that failed in the plumbing (no receipt, the "
        "lane's wait expired, client timeout); archive their first attempt.",
    )
    parser.add_argument("--ticket", default="OMN-20844")
    parser.add_argument("--lane", default="omn20844-bnc2")
    parser.add_argument("--state-root", default=None)
    parser.add_argument(
        "--in-process",
        action="store_true",
        help="Use --bus inmemory --locus in-process when the deployed lane has no consumer.",
    )
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

    if args.rescore is not None:
        if args.write and args.only:
            parser.error("--rescore --write rewrites whole files; drop --only")
        return rescore_dir(args.rescore, write=args.write, only=args.only)
    if args.table is not None:
        return print_table(
            args.table, by_difficulty=args.by_difficulty, by_feature=args.by_feature
        )
    if args.rung is not None:
        return run_rung_benchmark(args)
    if args.path is None and args.backend is None:
        parser.error(
            "one of --path, --backend, --rung, --rescore or --table is required"
        )

    if args.resume and args.backend is None:
        parser.error("--resume requires --backend")
    if args.concurrency < 1:
        parser.error("--concurrency must be positive")
    out = args.out
    if args.backend:
        out = out or RESULTS / f"{args.set}-{args.backend}.jsonl"
        try:
            check_results_file(out, resume=args.resume)
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    registry_root = os.environ.get("OMNI_HOME")
    needs_onex = args.backend is None or BACKENDS[args.backend].kind == "onex_delegate"
    if needs_onex and not registry_root:
        print("OMNI_HOME must be set; there is no default.", file=sys.stderr)
        return 2
    onex = Path(registry_root or ".") / "omnibase_infra" / "scripts" / "onex"
    if needs_onex and not onex.is_file():
        print(f"delegate CLI not found at {onex}", file=sys.stderr)
        return 2

    if (
        args.backend
        and BACKENDS[args.backend].kind == "harness"
        and not os.environ.get("OMNI_PLUGIN_ROOT")
    ):
        print("OMNI_PLUGIN_ROOT must be set for harness backends", file=sys.stderr)
        return 2
    path = (
        (EnumPath.UNPINNED if args.backend == "unpinned" else EnumPath.PINNED)
        if args.backend
        else EnumPath(args.path)
    )
    bundles = load_bundles(
        args.only,
        args.set,
        args.classes,
        EnumDifficulty(args.difficulty) if args.difficulty else None,
    )
    if args.shard:
        k, n = (int(x) for x in args.shard.split("/"))
        bundles = [b for i, b in enumerate(bundles) if i % n == k]
    if not bundles:
        print("no bundles selected", file=sys.stderr)
        return 2

    if args.backend:
        assert out is not None
        if args.resume:
            bundles = remaining_bundles(bundles, completed_task_ids(out))
        transcripts = out.parent / "responses" / f"{args.set}-{args.backend}"
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        transcripts = RESULTS / "responses" / path.value
    print(f"{len(bundles)} tasks, path={path.value}, concurrency={args.concurrency}")
    records: list[ModelRunRecord] = []
    with ExitStack() as stack:
        journal = (
            stack.enter_context(out.open("a" if args.resume else "x", encoding="utf-8"))
            if args.backend and out is not None
            else None
        )
        pool = stack.enter_context(
            concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency)
        )
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
                backend=args.backend,
                in_process_overlay=args.in_process_overlay,
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
                    notes="harness_error: harness failure, not a model result"
                    if args.backend
                    else "harness failure, not a model result",
                    backend=args.backend,
                    difficulty=bundle.difficulty,
                    task_type=bundle.task_type,
                    accepted=False,
                    host=BACKENDS[args.backend].host_label
                    if args.backend and args.backend != "unpinned"
                    else None,
                )
            records.append(record)
            if journal is not None:
                journal.write(record.model_dump_json() + "\n")
                journal.flush()
                os.fsync(journal.fileno())
            print(
                f"  {record.task_id:<7} {record.score.outcome.value:<13} "
                f"score={record.score.score:.2f} leak={record.leak_fraction:.2f} "
                f"{record.wall_ms / 1000:6.1f}s  {record.score.detail[:90]}"
            )

    records.sort(key=lambda r: r.task_id)
    if args.backend:
        print(f"\nwrote {out}")
        return 0
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
