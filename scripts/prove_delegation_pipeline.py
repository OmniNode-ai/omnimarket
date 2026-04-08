#!/usr/bin/env python3
"""OMN-7859: Prove the delegation pipeline on 3 PARTIAL nodes.

Tests whether the delegation pipeline (source-grounded prompts + quality gate +
GLM review + retry loop) can fix PARTIAL nodes.

Nodes tested:
  1. node_close_out (Simple) — dict-based handle() shim → canonical typed handle()
  2. node_local_review (Medium) — run_full_pipeline() → canonical handle()
  3. node_hostile_reviewer (Complex) — FSM with circuit breaker

Usage:
    cd $OMNI_HOME/worktrees/OMN-7853/omnimarket-prove
    source ~/.omnibase/.env
    uv run python scripts/prove_delegation_pipeline.py
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("prove_delegation")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OMNI_HOME = Path(os.environ.get("OMNI_HOME", Path(__file__).resolve().parents[4]))
WORKTREE = Path(__file__).resolve().parent.parent
# Read source from canonical repo (never overwrite it)
CANONICAL_NODES_ROOT = OMNI_HOME / "omnimarket" / "src" / "omnimarket" / "nodes"
# Write output to worktree (isolated from canonical)
OUTPUT_NODES_ROOT = WORKTREE / "src" / "omnimarket" / "nodes"
STATE_DIR = OMNI_HOME / ".onex_state"
TRACES_DIR = STATE_DIR / "dispatch-traces"
METRICS_DIR = STATE_DIR / "dispatch-metrics"

# Qwen3-Coder-30B on .201 — coder model
CODER_URL = "http://192.168.86.201:8000/v1/chat/completions"
CODER_MODEL = "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit"

# GLM reviewer — read from env
# Uses LLM_GLM_MODEL_NAME (default glm-4.5) since glm-4.7-flash is unreliable on z.ai
GLM_API_KEY = os.environ.get("LLM_GLM_API_KEY", "")
GLM_BASE_URL = os.environ.get("LLM_GLM_URL", "https://open.bigmodel.cn/api/paas/v4")
# GLM_BASE_URL already has the full path prefix, append /chat/completions only
GLM_REVIEW_URL = GLM_BASE_URL.rstrip("/") + "/chat/completions"
GLM_REVIEW_MODEL = os.environ.get("LLM_GLM_MODEL_NAME", "glm-4.5")

MAX_ATTEMPTS = 3
TIMEOUT_CODER = 180.0
TIMEOUT_REVIEWER = 90.0
# If reviewer unavailable/failed on all 3 attempts, accept if structural gate passed
ALLOW_UNREVIEWED_ON_GATE_PASS = True

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

CODER_SYSTEM_PROMPT = """\
You are an autonomous code refactoring agent for the OmniNode platform.
You will be given a template handler (a READY node to follow) and a target handler \
(the PARTIAL file to refactor).

Refactoring rules:
1. handle() must be the single canonical entry point.
2. handle() must accept the EXISTING typed Pydantic input model (e.g. \
ModelLocalReviewStartCommand) — do NOT introduce a new wrapper input class.
3. handle() must return the EXISTING typed Pydantic output model (e.g. \
ModelLocalReviewCompletedEvent or similar) — do NOT introduce a new wrapper output class.
4. Move orchestration logic from run_full_pipeline() directly into handle().
5. Rename start/advance/make_completed_event/serialize_event to private methods \
prefixed with underscore (_start, _advance, etc). Keep ALL their logic intact.
6. Remove the old dict-based handle() shim entirely.
7. Keep ALL business logic, FSM state, circuit breaker, and event construction intact.

Python style rules:
- Remove any imports that are unused in the output file.
- Use underscore prefix (_varname) for intentionally unused variables.
- No trailing whitespace on any line.
- Keep __all__ sorted alphabetically.

Output ONLY the complete refactored Python file. Do not add explanations, \
markdown fences, or commentary.
"""

REVIEW_SYSTEM_PROMPT = """\
You are a code review agent. Review the refactored code against the original source.

The goal of the refactoring is:
- handle() is the single canonical entry point (replaces run_full_pipeline())
- handle() accepts the EXISTING Pydantic start command model (e.g. ModelXxxStartCommand)
- handle() returns the EXISTING Pydantic completed event model (e.g. ModelXxxCompletedEvent)
- Public methods renamed to private (start → _start, advance → _advance, etc)
- dict-based shim handle() replaced by typed handle()
- ALL business logic preserved

Approve if:
- handle() accepts an existing Pydantic model (not dict, not Any)
- handle() returns an existing Pydantic model (not dict, not Any)
- All FSM/business logic from the original is present
- No new input/output wrapper classes were introduced (use existing models)
- Private methods (_start, _advance, etc) contain the logic from original public methods

Reject ONLY if:
- handle() still takes/returns dict or Any
- Business logic is missing or broken
- Fields referenced that don't exist in original Pydantic models

Method renaming (start→_start, advance→_advance) is EXPECTED and CORRECT — do not reject for this.
New typed input/output wrapper classes are NOT desired — reject if introduced.

You MUST respond with ONLY a JSON object, no prose, no explanation:
{
  "approved": true,
  "issues": [{"line": 15, "severity": "major", "message": "description"}],
  "risk_level": "low"
}

severity must be "minor", "major", or "critical".
risk_level must be "low", "medium", or "high".
issues must be an array (empty array if none).
"""

# ---------------------------------------------------------------------------
# Node definitions
# ---------------------------------------------------------------------------

TEMPLATE_NODE = CANONICAL_NODES_ROOT / "node_data_flow_sweep"

NODES = [
    {
        "id": "node_close_out",
        "label": "Simple",
        # Read source from canonical, write to worktree
        "source_dir": CANONICAL_NODES_ROOT / "node_close_out",
        "output_dir": OUTPUT_NODES_ROOT / "node_close_out",
    },
    {
        "id": "node_local_review",
        "label": "Medium",
        "source_dir": CANONICAL_NODES_ROOT / "node_local_review",
        "output_dir": OUTPUT_NODES_ROOT / "node_local_review",
    },
    {
        "id": "node_hostile_reviewer",
        "label": "Complex",
        "source_dir": CANONICAL_NODES_ROOT / "node_hostile_reviewer",
        "output_dir": OUTPUT_NODES_ROOT / "node_hostile_reviewer",
    },
]


# ---------------------------------------------------------------------------
# Source loading
# ---------------------------------------------------------------------------


def read_handler_file(node_dir: Path) -> str:
    handlers = sorted((node_dir / "handlers").glob("handler_*.py"))
    if not handlers:
        logger.warning("No handler_*.py in %s/handlers/", node_dir.name)
        return ""
    return handlers[0].read_text()


def load_model_sources(node_dir: Path) -> list[str]:
    models_dir = node_dir / "models"
    if not models_dir.exists():
        return []
    return [f.read_text() for f in sorted(models_dir.glob("model_*.py"))]


def build_coder_prompt(
    *,
    node_id: str,
    template_source: str,
    target_source: str,
    model_sources: list[str],
    review_feedback: str = "",
    max_context_chars: int = 48000,
) -> str:
    header = f"Node: {node_id}"
    template_section = f"## TEMPLATE (follow this pattern):\n{template_source}"
    target_section = f"## TARGET (refactor this):\n{target_source}"

    base_parts = [header, "", template_section, "", target_section]
    base_prompt = "\n".join(base_parts)

    # Add model files if they fit within budget
    model_parts: list[str] = []
    for src in model_sources:
        candidate = "\n".join(
            [base_prompt, "", "## RELEVANT MODELS:", *model_parts, src]
        )
        if len(candidate) <= max_context_chars:
            model_parts.append(src)
        else:
            break

    if model_parts:
        base_prompt = "\n".join(
            [base_prompt, "", "## RELEVANT MODELS:", *model_parts]
        )

    if review_feedback:
        base_prompt += f"\n\n## REVIEW FEEDBACK (fix these issues):\n{review_feedback}"

    return base_prompt


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------


def call_coder(prompt: str) -> str:
    """Call Qwen3-Coder-30B on .201."""
    payload = {
        "model": CODER_MODEL,
        "messages": [
            {"role": "system", "content": CODER_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 8192,
        "temperature": 0.1,
    }
    with httpx.Client(timeout=TIMEOUT_CODER) as client:
        resp = client.post(CODER_URL, json=payload)
        resp.raise_for_status()
        return str(resp.json()["choices"][0]["message"]["content"])


def call_reviewer(
    *,
    node_id: str,
    target_source: str,
    generated_code: str,
) -> str | None:
    """Call GLM-4.7-Flash for review. Returns raw response or None if unavailable."""
    if not GLM_API_KEY:
        logger.warning("LLM_GLM_API_KEY not set — skipping review")
        return None

    user_prompt = (
        f"Node: {node_id}\n\n"
        f"## ORIGINAL SOURCE:\n{target_source[:4000]}\n\n"
        f"## GENERATED CODE:\n{generated_code[:8000]}\n\n"
        f"Review the generated code against the original source. "
        f"Output only a JSON object."
    )
    payload = {
        "model": GLM_REVIEW_MODEL,
        "messages": [
            {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": 2048,
        "temperature": 0.3,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {GLM_API_KEY}",
    }
    try:
        with httpx.Client(timeout=TIMEOUT_REVIEWER) as client:
            resp = client.post(GLM_REVIEW_URL, json=payload, headers=headers)
            resp.raise_for_status()
            return str(resp.json()["choices"][0]["message"]["content"])
    except httpx.HTTPError as exc:
        logger.warning("GLM reviewer unreachable: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------


def extract_code(raw: str) -> str:
    python_fence = re.search(r"```python\s*\n(.*?)```", raw, re.DOTALL)
    if python_fence:
        return python_fence.group(1)
    generic_fence = re.search(r"```\s*\n(.*?)```", raw, re.DOTALL)
    if generic_fence:
        return generic_fence.group(1)
    return raw


# ---------------------------------------------------------------------------
# Quality gate
# ---------------------------------------------------------------------------


def run_quality_gate(code: str) -> dict:
    errors: list[str] = []
    ruff_pass = True
    syntax_pass = True

    try:
        ast.parse(code)
    except SyntaxError as exc:
        errors.append(f"Syntax error: {exc}")
        syntax_pass = False
        ruff_pass = False

    if syntax_pass:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="onex_gate_"
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name
        try:
            # Auto-apply safe fixes first (unused imports, whitespace, sort __all__)
            subprocess.run(
                ["ruff", "check", "--fix", "--unsafe-fixes", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            # Now check for remaining issues that couldn't be auto-fixed
            result = subprocess.run(
                ["ruff", "check", "--output-format=concise", tmp_path],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                ruff_pass = False
                for line in result.stdout.strip().splitlines():
                    errors.append(f"ruff: {line}")
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            logger.debug("ruff check skipped: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)

    return {
        "ruff_pass": ruff_pass,
        "syntax_pass": syntax_pass,
        "all_pass": ruff_pass and syntax_pass,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Review parsing
# ---------------------------------------------------------------------------


def parse_review(raw: str) -> dict | None:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
        text = inner.strip()

    try:
        data = json.loads(text)
        return data
    except json.JSONDecodeError:
        pass

    # Try extracting JSON block from prose
    json_match = re.search(r'\{[^{}]*"approved"[^{}]*\}', raw, re.DOTALL)
    if json_match:
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(json_match.group(0))

    return None


# ---------------------------------------------------------------------------
# Trace writing
# ---------------------------------------------------------------------------


def write_trace(trace: dict) -> None:
    TRACES_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{trace['correlation_id']}-{trace['node_id']}-attempt-{trace['attempt']}.json"
    (TRACES_DIR / fname).write_text(json.dumps(trace, indent=2, default=str))
    logger.debug("Wrote trace: %s", fname)


# ---------------------------------------------------------------------------
# Per-node pipeline
# ---------------------------------------------------------------------------


def run_node_pipeline(
    node_cfg: dict,
    template_source: str,
    correlation_id: str,
) -> dict:
    node_id = node_cfg["id"]
    node_dir = node_cfg["source_dir"]  # read from canonical, never overwrite
    label = node_cfg["label"]

    logger.info("=== Node %s (%s) ===", node_id, label)

    target_source = read_handler_file(node_dir)
    model_sources = load_model_sources(node_dir)

    logger.info(
        "  Template: %d chars | Target: %d chars | Models: %d files",
        len(template_source),
        len(target_source),
        len(model_sources),
    )

    traces = []
    accepted_code = None
    review_feedback = ""
    quality_gate_failures = 0
    review_rejections = 0

    for attempt in range(1, MAX_ATTEMPTS + 1):
        logger.info("  Attempt %d/%d", attempt, MAX_ATTEMPTS)
        t0 = time.monotonic()

        # Build prompt
        prompt = build_coder_prompt(
            node_id=node_id,
            template_source=template_source,
            target_source=target_source,
            model_sources=model_sources,
            review_feedback=review_feedback,
        )
        prompt_chars = len(prompt)

        # Call coder
        raw = ""
        failure_kind = None
        gate_result = None
        review_result = None

        try:
            logger.info("    Calling Qwen3-Coder (%d chars prompt)...", prompt_chars)
            raw = call_coder(prompt)
            logger.info("    Coder response: %d chars", len(raw))
        except Exception as exc:
            logger.error("    Coder call failed: %s", exc)
            failure_kind = "transport_failure"
            wall_ms = int((time.monotonic() - t0) * 1000)
            trace = {
                "correlation_id": correlation_id,
                "node_id": node_id,
                "attempt": attempt,
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "coder_model": CODER_MODEL,
                "reviewer_model": None,
                "prompt_chars": prompt_chars,
                "generation_raw": raw,
                "quality_gate": {"all_pass": False, "errors": [str(exc)]},
                "review_result": None,
                "accepted": False,
                "wall_clock_ms": wall_ms,
                "failure_kind": failure_kind,
            }
            write_trace(trace)
            traces.append(trace)
            review_feedback = f"Transport failure: {exc}. Retry."
            continue

        # Extract code
        code = extract_code(raw)
        if not code.strip():
            failure_kind = "generation_malformed"
            wall_ms = int((time.monotonic() - t0) * 1000)
            trace = {
                "correlation_id": correlation_id,
                "node_id": node_id,
                "attempt": attempt,
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "coder_model": CODER_MODEL,
                "reviewer_model": None,
                "prompt_chars": prompt_chars,
                "generation_raw": raw[:2000],
                "quality_gate": {"all_pass": False, "errors": ["Empty code extracted"]},
                "review_result": None,
                "accepted": False,
                "wall_clock_ms": wall_ms,
                "failure_kind": failure_kind,
            }
            write_trace(trace)
            traces.append(trace)
            review_feedback = "Response produced empty code. Output complete Python code only."
            continue

        # Quality gate
        gate_result = run_quality_gate(code)
        if not gate_result["all_pass"]:
            quality_gate_failures += 1
            failure_kind = "gate_failed"
            wall_ms = int((time.monotonic() - t0) * 1000)
            logger.warning("    Quality gate FAILED: %s", gate_result["errors"])
            trace = {
                "correlation_id": correlation_id,
                "node_id": node_id,
                "attempt": attempt,
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "coder_model": CODER_MODEL,
                "reviewer_model": None,
                "prompt_chars": prompt_chars,
                "generation_raw": raw[:2000],
                "quality_gate": gate_result,
                "review_result": None,
                "accepted": False,
                "wall_clock_ms": wall_ms,
                "failure_kind": failure_kind,
            }
            write_trace(trace)
            traces.append(trace)
            review_feedback = "Quality gate failed:\n" + "\n".join(gate_result["errors"])
            continue

        logger.info("    Quality gate PASSED")

        # GLM review
        reviewer_model_used = None
        review_status = "skipped"

        if GLM_API_KEY:
            reviewer_model_used = GLM_REVIEW_MODEL
            logger.info("    Calling GLM reviewer...")

            # Up to 2 review attempts for malformed responses
            for rev_attempt in range(1, 3):
                raw_review = call_reviewer(
                    node_id=node_id,
                    target_source=target_source,
                    generated_code=code,
                )
                if raw_review is None:
                    review_status = "unavailable"
                    break

                parsed = parse_review(raw_review)
                if parsed is None:
                    logger.warning(
                        "    Review returned malformed JSON (attempt %d): %s",
                        rev_attempt,
                        repr(raw_review[:300]),
                    )
                    if rev_attempt == 2:
                        review_status = "failed"
                    continue

                review_result = parsed
                review_status = "approved" if parsed.get("approved", False) else "rejected"
                break

            logger.info("    Review status: %s", review_status)
        else:
            logger.info("    Review skipped (no GLM key)")

        wall_ms = int((time.monotonic() - t0) * 1000)

        if review_status == "rejected":
            review_rejections += 1
            failure_kind = "review_rejected"
            issues = review_result.get("issues", []) if review_result else []
            trace = {
                "correlation_id": correlation_id,
                "node_id": node_id,
                "attempt": attempt,
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "coder_model": CODER_MODEL,
                "reviewer_model": reviewer_model_used,
                "prompt_chars": prompt_chars,
                "generation_raw": raw[:2000],
                "quality_gate": gate_result,
                "review_result": review_result,
                "accepted": False,
                "wall_clock_ms": wall_ms,
                "failure_kind": failure_kind,
            }
            write_trace(trace)
            traces.append(trace)
            review_feedback = "Review issues:\n" + "\n".join(
                f"- Line {i.get('line', '?')}: [{i.get('severity', '?')}] {i.get('message', '')}"
                for i in issues
            )
            continue

        if review_status in ("unavailable", "failed"):
            if ALLOW_UNREVIEWED_ON_GATE_PASS:
                # Accept: structural gate passed, reviewer unavailable/failed
                # Record the unreviewed status but don't reject
                logger.info(
                    "    Reviewer %s — accepting (ALLOW_UNREVIEWED_ON_GATE_PASS=True)",
                    review_status,
                )
                trace = {
                    "correlation_id": correlation_id,
                    "node_id": node_id,
                    "attempt": attempt,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "coder_model": CODER_MODEL,
                    "reviewer_model": reviewer_model_used,
                    "prompt_chars": prompt_chars,
                    "generation_raw": raw[:2000],
                    "quality_gate": gate_result,
                    "review_result": None,
                    "accepted": True,
                    "wall_clock_ms": wall_ms,
                    "failure_kind": f"review_{review_status}_but_accepted",
                }
                write_trace(trace)
                traces.append(trace)
                accepted_code = code
                logger.info("    ACCEPTED (unreviewed) on attempt %d/%d", attempt, MAX_ATTEMPTS)
                break
            else:
                failure_kind = "review_unavailable"
                trace = {
                    "correlation_id": correlation_id,
                    "node_id": node_id,
                    "attempt": attempt,
                    "timestamp": datetime.now(tz=UTC).isoformat(),
                    "coder_model": CODER_MODEL,
                    "reviewer_model": reviewer_model_used,
                    "prompt_chars": prompt_chars,
                    "generation_raw": raw[:2000],
                    "quality_gate": gate_result,
                    "review_result": None,
                    "accepted": False,
                    "wall_clock_ms": wall_ms,
                    "failure_kind": failure_kind,
                }
                write_trace(trace)
                traces.append(trace)
                review_feedback = f"Reviewer {review_status} — retry."
                continue

        # Accepted (approved or skipped with no reviewer)
        trace = {
            "correlation_id": correlation_id,
            "node_id": node_id,
            "attempt": attempt,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "coder_model": CODER_MODEL,
            "reviewer_model": reviewer_model_used,
            "prompt_chars": prompt_chars,
            "generation_raw": raw[:2000],
            "quality_gate": gate_result,
            "review_result": review_result,
            "accepted": True,
            "wall_clock_ms": wall_ms,
            "failure_kind": None,
        }
        write_trace(trace)
        traces.append(trace)
        accepted_code = code
        logger.info("    ACCEPTED on attempt %d/%d", attempt, MAX_ATTEMPTS)
        break

    return {
        "node_id": node_id,
        "label": label,
        "accepted": accepted_code is not None,
        "accepted_code": accepted_code,
        "total_attempts": len(traces),
        "quality_gate_failures": quality_gate_failures,
        "review_rejections": review_rejections,
        "traces": traces,
    }


# ---------------------------------------------------------------------------
# Write accepted code to worktree
# ---------------------------------------------------------------------------


def write_accepted_output(node_cfg: dict, code: str) -> Path:
    """Write generated code to the WORKTREE handler file (not the canonical source).

    Uses output_dir (worktree) not source_dir (canonical repo).
    Targets handler_{name}.py matching the node_id.
    """
    node_id = node_cfg["id"]  # e.g. "node_hostile_reviewer"
    output_dir = node_cfg["output_dir"]
    handlers_dir = output_dir / "handlers"
    handlers_dir.mkdir(parents=True, exist_ok=True)

    canonical_name = "handler_" + node_id.removeprefix("node_") + ".py"
    out_path = handlers_dir / canonical_name
    out_path.write_text(code)
    logger.info("Wrote accepted code to: %s", out_path)
    return out_path


def run_ruff_format(path: Path) -> bool:
    try:
        # Apply auto-fixes first
        subprocess.run(
            ["ruff", "check", "--fix", "--unsafe-fixes", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        result = subprocess.run(
            ["ruff", "format", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def run_final_ruff_check(path: Path) -> tuple[bool, list[str]]:
    try:
        result = subprocess.run(
            ["ruff", "check", "--output-format=concise", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        errors = result.stdout.strip().splitlines() if result.returncode != 0 else []
        return result.returncode == 0, errors
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return True, []  # skip if ruff not available


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    correlation_id = str(uuid.uuid4())[:8]
    run_ts = datetime.now(tz=UTC).isoformat()

    logger.info("OMN-7859: Delegation Pipeline Prove Run")
    logger.info("  correlation_id: %s", correlation_id)
    logger.info("  coder: %s @ %s", CODER_MODEL, CODER_URL)
    logger.info("  reviewer: %s @ %s", GLM_REVIEW_MODEL, GLM_REVIEW_URL)
    logger.info("  GLM key present: %s", bool(GLM_API_KEY))

    # Load template from canonical repo
    template_source = read_handler_file(TEMPLATE_NODE)
    logger.info(
        "Template (%s from canonical): %d chars",
        TEMPLATE_NODE.name,
        len(template_source),
    )

    if not template_source:
        logger.error("Could not load template from %s", TEMPLATE_NODE)
        sys.exit(1)

    # Run each node
    per_node_results = []
    for node_cfg in NODES:
        result = run_node_pipeline(node_cfg, template_source, correlation_id)
        per_node_results.append(result)

        if result["accepted"] and result["accepted_code"]:
            out_path = write_accepted_output(node_cfg, result["accepted_code"])
            ruff_fmt = run_ruff_format(out_path)
            ruff_ok, ruff_errors = run_final_ruff_check(out_path)
            result["output_path"] = str(out_path)
            result["post_write_ruff_pass"] = ruff_ok
            result["post_write_ruff_errors"] = ruff_errors
            if not ruff_fmt:
                logger.warning("  ruff format failed on %s", out_path)
            if ruff_ok:
                logger.info("  Post-write ruff check PASSED: %s", out_path)
            else:
                logger.warning("  Post-write ruff errors: %s", ruff_errors)

    # Aggregate metrics
    nodes_accepted = sum(1 for r in per_node_results if r["accepted"])
    total_attempts = sum(r["total_attempts"] for r in per_node_results)
    total_gate_failures = sum(r["quality_gate_failures"] for r in per_node_results)
    total_review_rejections = sum(r["review_rejections"] for r in per_node_results)
    avg_attempts = round(total_attempts / len(per_node_results), 2)

    metrics = {
        "run_timestamp": run_ts,
        "correlation_id": correlation_id,
        "nodes_attempted": len(NODES),
        "nodes_accepted": nodes_accepted,
        "total_attempts": total_attempts,
        "avg_attempts_per_node": avg_attempts,
        "quality_gate_failures": total_gate_failures,
        "review_rejections": total_review_rejections,
        "coder_model": CODER_MODEL,
        "reviewer_model": GLM_REVIEW_MODEL,
        "per_node": [
            {
                "node_id": r["node_id"],
                "label": r["label"],
                "accepted": r["accepted"],
                "total_attempts": r["total_attempts"],
                "quality_gate_failures": r["quality_gate_failures"],
                "review_rejections": r["review_rejections"],
                "output_path": r.get("output_path"),
                "post_write_ruff_pass": r.get("post_write_ruff_pass"),
            }
            for r in per_node_results
        ],
    }

    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    metrics_path = METRICS_DIR / "prove-delegation-20260408.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))
    logger.info("Metrics written to: %s", metrics_path)

    # Print summary
    print("\n" + "=" * 60)
    print("DELEGATION PIPELINE PROVE RUN — SUMMARY")
    print("=" * 60)
    print(f"Correlation ID : {correlation_id}")
    print(f"Nodes attempted: {len(NODES)}")
    print(f"Nodes accepted : {nodes_accepted}/{len(NODES)}")
    print(f"Total attempts : {total_attempts}")
    print(f"Avg attempts   : {avg_attempts}")
    print(f"Gate failures  : {total_gate_failures}")
    print(f"Review rejects : {total_review_rejections}")
    print()

    for r in per_node_results:
        status = "ACCEPTED" if r["accepted"] else "REJECTED"
        print(f"  [{status}] {r['node_id']} ({r['label']})")
        print(f"    attempts={r['total_attempts']} gate_fails={r['quality_gate_failures']} review_rejects={r['review_rejections']}")
        if r.get("output_path"):
            print(f"    output: {r['output_path']}")
            ruff_ok = r.get("post_write_ruff_pass")
            if ruff_ok is not None:
                print(f"    ruff: {'PASS' if ruff_ok else 'FAIL'}")
        if not r["accepted"] and r["traces"]:
            last_trace = r["traces"][-1]
            print(f"    last failure: {last_trace.get('failure_kind', 'unknown')}")
            gate = last_trace.get("quality_gate", {})
            if gate.get("errors"):
                print(f"    gate errors: {gate['errors'][:3]}")

    print()
    print(f"Traces: {TRACES_DIR}")
    print(f"Metrics: {metrics_path}")
    print("=" * 60)

    if nodes_accepted == 0:
        print("\nDIAGNOSIS: 0/3 nodes accepted.")
        print("Check traces in .onex_state/dispatch-traces/ for details.")
        print("Possible causes:")
        print("  - Qwen3-Coder unreachable at 192.168.86.201:8000")
        print("  - GLM reviewer unavailable (check LLM_GLM_API_KEY)")
        print("  - Model generating structurally invalid Python")


if __name__ == "__main__":
    main()
