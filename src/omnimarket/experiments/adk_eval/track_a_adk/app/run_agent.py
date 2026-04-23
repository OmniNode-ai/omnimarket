# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Track A runner — drives the ADK agent over the P5 input findings.

Uses the ADK `Runner` + `InMemorySessionService` to invoke the agent with a
single user turn whose content is the JSON-encoded list of mypy findings from
`input_findings.jsonl`. Collects the final text response, strips optional
markdown fences, and writes a JSON payload to `track_a_output.json` that the
harness can subsequently validate as `ModelTypeDebtReport`.

Metrics collected:
- wall-clock latency per run (median over 5 runs)
- token usage (input/output) from the underlying `google-genai` response

This module runs inside the dedicated ``track_a_adk/.venv`` because the ADK
package conflicts with the omnimarket venv's opentelemetry pin.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "False")

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agent import root_agent

_APP_NAME = "track_a_adk"
_USER_ID = "track_a_runner"


def _load_findings(jsonl_path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for raw in jsonl_path.read_text().splitlines():
        line = raw.strip()
        if not line:
            continue
        findings.append(json.loads(line))
    return findings


def _strip_fences(text: str) -> str:
    """Remove ```json / ``` markdown fences if the model included them."""
    s = text.strip()
    if s.startswith("```"):
        # drop first fence line
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        # drop trailing fence
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


async def _one_run(findings: list[dict[str, Any]], repo_label: str) -> dict[str, Any]:
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=_APP_NAME,
        user_id=_USER_ID,
    )
    runner = Runner(
        app_name=_APP_NAME,
        agent=root_agent,
        session_service=session_service,
    )

    user_payload = (
        f"Prioritize the following {len(findings)} mypy findings from repo "
        f"'{repo_label}'. Return only the ModelTypeDebtReport JSON.\n\n"
        f"Findings:\n{json.dumps(findings, indent=2)}"
    )
    content = types.Content(role="user", parts=[types.Part(text=user_payload)])

    input_tokens = 0
    output_tokens = 0
    llm_calls = 0
    final_text = ""
    start = time.perf_counter()
    async for event in runner.run_async(
        user_id=_USER_ID,
        session_id=session.id,
        new_message=content,
    ):
        usage = getattr(event, "usage_metadata", None)
        if usage is not None:
            llm_calls += 1
            input_tokens += getattr(usage, "prompt_token_count", 0) or 0
            output_tokens += getattr(usage, "candidates_token_count", 0) or 0
        if event.is_final_response() and event.content and event.content.parts:
            for part in event.content.parts:
                if getattr(part, "text", None):
                    final_text += part.text
    latency = time.perf_counter() - start

    return {
        "latency_seconds": latency,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "llm_calls": llm_calls,
        "final_text": final_text,
    }


async def main() -> int:
    input_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    metrics_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    num_runs = int(sys.argv[4]) if len(sys.argv) > 4 else 5

    findings = _load_findings(input_path)
    repo_label = "omnibase_core"  # P5 sample is sourced from omnibase_core

    per_run: list[dict[str, Any]] = []
    final_payload_text: str | None = None

    for i in range(num_runs):
        print(f"[track-a] run {i + 1}/{num_runs}...", flush=True)
        result = await _one_run(findings, repo_label)
        per_run.append(result)
        # Keep the last non-empty response for the canonical output.
        if result["final_text"].strip():
            final_payload_text = result["final_text"]

    if final_payload_text is None:
        print("[track-a] no final text from any run", file=sys.stderr)
        return 2

    latencies = [r["latency_seconds"] for r in per_run]
    input_total = sum(r["input_tokens"] for r in per_run)
    output_total = sum(r["output_tokens"] for r in per_run)

    # Parse the last response text as JSON to validate shape locally.
    parsed = json.loads(_strip_fences(final_payload_text))

    # Overwrite the LLM-generated placeholder metrics with real ones from the
    # final-recorded run (authoritative source for the track's single-run
    # performance number).
    last = per_run[-1]
    parsed["latency_seconds"] = last["latency_seconds"]
    parsed["llm_calls"] = max(last["llm_calls"], 1)
    # Gemini 2.5 Flash pricing (AI Studio, 2026-04): $0.30/M input, $2.50/M output.
    parsed["estimated_cost_usd"] = (
        last["input_tokens"] * 0.30 / 1_000_000
        + last["output_tokens"] * 2.50 / 1_000_000
    )
    parsed["findings_total"] = len(findings)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(parsed, indent=2) + "\n")
    print(
        f"[track-a] wrote {output_path} ({len(parsed.get('findings_prioritized', []))} prioritized)"
    )

    if metrics_path is not None:
        metrics = {
            "runs": num_runs,
            "latency_seconds_median": statistics.median(latencies),
            "latency_seconds_min": min(latencies),
            "latency_seconds_max": max(latencies),
            "latencies_all": latencies,
            "input_tokens_total": input_total,
            "output_tokens_total": output_total,
            "input_tokens_per_run": [r["input_tokens"] for r in per_run],
            "output_tokens_per_run": [r["output_tokens"] for r in per_run],
            "llm_calls_per_run": [r["llm_calls"] for r in per_run],
            "model": "gemini-flash-latest",
            "auth_path": "ai_studio",
            "estimated_cost_usd_per_run_median": (
                statistics.median(
                    (
                        r["input_tokens"] * 0.30 / 1_000_000
                        + r["output_tokens"] * 2.50 / 1_000_000
                    )
                    for r in per_run
                )
            ),
        }
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        metrics_path.write_text(json.dumps(metrics, indent=2) + "\n")
        print(f"[track-a] wrote metrics to {metrics_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
