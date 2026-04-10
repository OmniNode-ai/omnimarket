# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_overseer_verifier.

Runs the deterministic 5-check verification gate against a ticket or PR.

Usage:
    python -m omnimarket.nodes.node_overseer_verifier --ticket OMN-1234
    python -m omnimarket.nodes.node_overseer_verifier --pr omnimarket#186
    python -m omnimarket.nodes.node_overseer_verifier --ticket OMN-1234 --dry-run

The node_id and domain are derived from the target. For --ticket the domain is
"ticket_pipeline" and the node_id is "node_ticket_pipeline". For --pr the domain
is "build_loop" and the node_id is "node_build_loop_orchestrator".

Outputs JSON to stdout: verdict, checks, failure_class, summary.
Exit 0 = PASS, exit 1 = FAIL or ESCALATE.

Related:
    - OMN-8031: node_overseer_verifier in omnimarket
    - OMN-8025: Overseer seam integration epic
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from omnimarket.nodes.node_overseer_verifier.handlers.handler_overseer_verifier import (
    HandlerOverseerVerifier,
)
from omnimarket.nodes.node_overseer_verifier.models.model_verifier_request import (
    ModelVerifierRequest,
)

_log = logging.getLogger(__name__)


def _parse_pr(pr_arg: str) -> tuple[str, str]:
    """Parse '<repo>#<num>' into (repo, number) strings."""
    if "#" not in pr_arg:
        msg = f"--pr must be in '<repo>#<num>' format, got: {pr_arg!r}"
        raise argparse.ArgumentTypeError(msg)
    repo, _, num = pr_arg.partition("#")
    if not repo or not num.isdigit():
        msg = f"--pr must be in '<repo>#<num>' format, got: {pr_arg!r}"
        raise argparse.ArgumentTypeError(msg)
    return repo, num


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Run the overseer verifier deterministic 5-check gate "
            "against a ticket or PR."
        )
    )

    target_group = parser.add_mutually_exclusive_group(required=True)
    target_group.add_argument(
        "--ticket",
        metavar="OMN-XXXX",
        help="Linear ticket ID to verify (e.g. OMN-1234)",
    )
    target_group.add_argument(
        "--pr",
        metavar="REPO#NUM",
        help="GitHub PR in '<repo>#<num>' format (e.g. omnimarket#186)",
    )

    parser.add_argument(
        "--status",
        default="completed",
        help="Task status to report (default: completed)",
    )
    parser.add_argument(
        "--runner-id",
        default=None,
        help="Runner ID that executed the task (optional)",
    )
    parser.add_argument(
        "--attempt",
        type=int,
        default=1,
        help="Attempt number (default: 1)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=None,
        help="Model confidence score 0.0-1.0 (optional)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Print what would be verified without writing output files",
    )
    parser.add_argument(
        "--json",
        dest="output_json",
        action="store_true",
        default=False,
        help="Output raw JSON (default: human-readable summary)",
    )

    args = parser.parse_args()

    # Derive task_id, domain, node_id from target
    if args.ticket is not None:
        task_id = args.ticket
        domain = "ticket_pipeline"
        node_id = "node_ticket_pipeline"
    else:
        repo, num = _parse_pr(args.pr)
        task_id = f"{repo}#{num}"
        domain = "build_loop"
        node_id = "node_build_loop_orchestrator"

    if args.dry_run:
        sys.stdout.write(
            json.dumps(
                {
                    "dry_run": True,
                    "task_id": task_id,
                    "domain": domain,
                    "node_id": node_id,
                    "status": args.status,
                },
                indent=2,
            )
            + "\n"
        )
        sys.exit(0)

    request = ModelVerifierRequest(
        task_id=task_id,
        status=args.status,
        domain=domain,
        node_id=node_id,
        runner_id=args.runner_id,
        attempt=args.attempt,
        confidence=args.confidence,
    )

    handler = HandlerOverseerVerifier()
    result = handler.verify(request)

    verdict = str(result.get("verdict", "FAIL"))
    checks = result.get("checks", [])
    summary = str(result.get("summary", ""))
    failure_class = result.get("failure_class")

    if args.output_json:
        sys.stdout.write(json.dumps(result, indent=2) + "\n")
    else:
        _render_human(task_id, verdict, checks, failure_class, summary)

    sys.exit(0 if verdict == "PASS" else 1)


def _render_human(
    task_id: str,
    verdict: str,
    checks: object,
    failure_class: object,
    summary: str,
) -> None:
    """Print a human-readable verification report."""
    verdict_icon = {"PASS": "PASS", "FAIL": "FAIL", "ESCALATE": "ESCALATE"}.get(
        verdict, verdict
    )
    sys.stdout.write(f"\nOverseer Verification — {task_id}\n")
    sys.stdout.write("=" * 50 + "\n\n")
    sys.stdout.write(f"Verdict: {verdict_icon}\n")
    if failure_class:
        sys.stdout.write(f"Failure class: {failure_class}\n")
    sys.stdout.write(f"Summary: {summary}\n\n")

    if isinstance(checks, list) and checks:
        sys.stdout.write("Check Results:\n")
        for check in checks:
            if isinstance(check, dict):
                icon = "PASS" if check.get("passed") else "FAIL"
                name = check.get("name", "unknown")
                msg = check.get("message", "")
                line = f"  [{icon}] {name}"
                if msg:
                    line += f" — {msg}"
                sys.stdout.write(line + "\n")

    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
