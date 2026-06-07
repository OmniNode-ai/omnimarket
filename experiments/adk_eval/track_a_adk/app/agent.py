# ruff: noqa
# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Track A ADK agent — Gemini 2.5 Flash type-debt scout.

AI Studio auth path (GEMINI_API_KEY / GOOGLE_API_KEY). Single tool
`run_mypy_and_parse` imported from the shared parser at
`experiments.adk_eval.tools.mypy_parser`.

Output contract: agent is prompted to return a JSON payload parseable as
`ModelTypeDebtReport` (the harness validates before writing).

SCOPE NOTE (OMN-12743): Track A lives in experiments/adk_eval/track_a_adk/
and runs in a dedicated venv isolated from the omnimarket demo bus. It is
classified as EXPERIMENT-ONLY — not on the live demo path. The model is
resolved from the TRACK_A_GEMINI_MODEL environment variable so the harness
operator can override without editing source. The env-var default
"gemini-flash-latest" is experiment-local and intentionally not routed
through contract/overlay config (Track A predates the ONEX contract layer
and runs outside the omnimarket node runtime).
"""

import os
from pathlib import Path
from typing import Any

# AI Studio — NOT Vertex. Scaffold default uses Vertex via google.auth.default();
# we override before importing ADK so it picks the AI Studio path.
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

# Model resolved from env var; "gemini-flash-latest" is the experiment default.
# Override via TRACK_A_GEMINI_MODEL env var before invoking the harness.
# experiment-model-literal-ok: OMN-12743 — Track A is experiment-only (not demo-path)
TRACK_A_GEMINI_MODEL: str = os.environ.get(
    "TRACK_A_GEMINI_MODEL", "gemini-flash-latest"
)


def run_mypy_and_parse(repo_path: str) -> list[dict[str, Any]]:
    """Run mypy --strict on a repo and return the parsed findings.

    Wraps the shared parser used by both Track A (this agent) and Track B
    (omnimarket POC), living at
    ``experiments.adk_eval.tools.mypy_parser``. Returns a list of
    dicts built from ``ModelMypyFinding.model_dump()``.

    Args:
        repo_path: Absolute path to the repo root to scan. Mypy runs against
            the ``src/`` subdirectory by default.

    Returns:
        List of mypy findings, each a dict with keys ``file``, ``line``,
        ``column``, ``severity``, ``error_code``, ``message``.
    """
    from experiments.adk_eval.tools.mypy_parser import (
        run_mypy_and_parse as _run,
    )

    findings = _run(Path(repo_path))
    return [f.model_dump(mode="json") for f in findings]


_SYSTEM_PROMPT = """You are a code quality prioritizer.

Given a list of mypy findings (each with `file`, `line`, `column`, `severity`,
`error_code`, `message`), classify each finding's priority as one of:
- "critical": production bug risk or type safety hole with runtime consequences
- "major": real type debt but contained; should be fixed soon
- "minor": stylistic or low-impact (e.g. unused type: ignore, missing annotations)
- "noise": not actionable (e.g. missing third-party stubs, test-only)

For each finding, produce a ModelTypeDebtPriority entry with:
- finding_ref: "<file>:<line>"
- priority: one of "critical"|"major"|"minor"|"noise"
- rationale: one-sentence justification
- fix_sketch: optional short fix suggestion (or null)

Output MUST be a single JSON object (no markdown fences, no prose) matching:
{
  "repo": "<string>",
  "generated_at": "<ISO 8601 UTC datetime>",
  "findings_total": <int>,
  "findings_prioritized": [ <ModelTypeDebtPriority>, ... ],
  "tool": "adk",
  "latency_seconds": 0.0,
  "llm_calls": 1,
  "estimated_cost_usd": 0.0
}

Do not wrap the JSON in markdown. Do not add commentary. Produce valid JSON only.
Duplicate finding_ref values are forbidden. Fill latency_seconds / llm_calls /
estimated_cost_usd with placeholders; the harness overwrites them.
"""


root_agent = Agent(
    name="type_debt_scout_adk",
    model=Gemini(
        model=TRACK_A_GEMINI_MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=_SYSTEM_PROMPT,
    tools=[run_mypy_and_parse],
)


app = App(
    root_agent=root_agent,
    name="track_a_adk",
)
