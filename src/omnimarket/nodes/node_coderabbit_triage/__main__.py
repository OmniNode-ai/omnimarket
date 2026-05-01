# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_coderabbit_triage.

Usage:
    python -m omnimarket.nodes.node_coderabbit_triage \
        --repo OmniNode-ai/omniclaude \
        --pr 42 \
        --dry-run

Outputs JSON to stdout: ModelCoderabbitTriageResult model.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from omnimarket.cli.args import (
    add_output_args,
    report_output_requested,
    resolve_output_config,
)
from omnimarket.cli.output.registry import resolve_handler
from omnimarket.cli.reporting import (
    build_report_from_model_result,
    load_contract_metadata,
)
from omnimarket.nodes.node_coderabbit_triage.handlers.handler_coderabbit_triage import (
    HandlerCoderabbitTriage,
    ModelCoderabbitTriageCommand,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage CodeRabbit review threads on a GitHub PR."
    )
    parser.add_argument("--repo", required=True, help="GitHub org/repo slug")
    parser.add_argument("--pr", type=int, required=True, help="PR number")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Classify but do not post replies or resolve threads",
    )
    add_output_args(parser)

    args = parser.parse_args()
    output_config = resolve_output_config(args)

    command = ModelCoderabbitTriageCommand(
        repo=args.repo,
        pr_number=args.pr,
        correlation_id=str(uuid.uuid4()),
        dry_run=args.dry_run,
    )

    handler = HandlerCoderabbitTriage()
    result = handler.handle(command)
    if not report_output_requested():
        sys.stdout.write(result.model_dump_json(indent=2) + "\n")
        return

    contract_name, contract_version, terminal_event = load_contract_metadata(
        "omnimarket.nodes.node_coderabbit_triage"
    )
    cli_report = build_report_from_model_result(
        result,
        skill_name="coderabbit_triage",
        node_name="node_coderabbit_triage",
        terminal_event=terminal_event,
        contract_name=contract_name,
        contract_version=contract_version,
        mode="dry_run" if args.dry_run else "execute",
        input_summary={
            "repo": args.repo,
            "pr_number": args.pr,
            "dry_run": args.dry_run,
        },
        output_config=output_config,
    )
    rendered = resolve_handler(output_config.format).render(cli_report)
    sys.stdout.write(rendered)
    if not rendered.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
