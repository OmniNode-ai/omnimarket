# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_llm_eval_harness.

Benchmarks a set of models against a fixed task corpus and prints results.

Usage:
    python -m omnimarket.nodes.node_llm_eval_harness \
        --models qwen3-coder-30b,deepseek-r1-14b \
        --task-types code_generation,classification \
        --dry-run

Outputs JSON to stdout: LlmEvalResult model with per-sample + summary rollup.
"""

from __future__ import annotations

import argparse
import json
import sys

from omnimarket.nodes.node_llm_eval_harness.handlers.handler_llm_eval_harness import (
    EnumLlmEvalTaskType,
    LlmEvalRequest,
    NodeLlmEvalHarness,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark LLM output quality per model and task type.",
    )
    parser.add_argument(
        "--models",
        required=True,
        help="Comma-separated model keys (must match model_registry.yaml)",
    )
    parser.add_argument(
        "--task-types",
        default="",
        help="Comma-separated EnumLlmEvalTaskType values (default: all)",
    )
    parser.add_argument(
        "--max-tasks-per-type",
        type=int,
        default=5,
        help="Cap tasks executed per (model, task_type) pair",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip LLM calls; emit a shaped zero-value result",
    )

    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    task_types: list[EnumLlmEvalTaskType] = []
    for raw in args.task_types.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            task_types.append(EnumLlmEvalTaskType(raw))
        except ValueError:
            sys.stderr.write(f"unknown task_type: {raw}\n")
            sys.exit(2)

    request = LlmEvalRequest(
        models=models,
        task_types=task_types,
        max_tasks_per_type=args.max_tasks_per_type,
        dry_run=args.dry_run,
    )

    handler = NodeLlmEvalHarness()
    result = handler.handle(request)

    payload = result.model_dump()
    payload["summary"] = result.summary
    sys.stdout.write(json.dumps(payload, indent=2, default=str) + "\n")

    if result.status == "error":
        sys.exit(1)


if __name__ == "__main__":
    main()
