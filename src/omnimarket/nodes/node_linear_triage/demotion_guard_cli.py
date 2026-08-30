# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headless runner for the git-automation demotion ratchet (OMN-16536 AC#2).

Runs with no human in the loop: enumerate every Linear team, read each team's
``gitAutomationStates`` individually, assert that none of them resolves a
``completed``-type ticket to a non-``completed`` state, print the report, and
exit non-zero on drift or on any failure of the positive control.

The scheduled runner is
``omnibase_infra/.github/workflows/linear-demotion-guard-armed.yml``
(OMN-16536 AC#2), which holds ``LINEAR_API_KEY`` and checks this repo out at
``dev``; this repo's own ``.github/workflows/linear-demotion-guard.yml`` is
``workflow_dispatch``/``workflow_call``-only because omnimarket has no Linear
credential (OMN-17182). Either way the assertion actually fires rather than
sitting in a doc — the setting is not the mechanism.

Requires:
  LINEAR_API_KEY — Linear API key with read access to team settings.

Usage:
    python -m omnimarket.nodes.node_linear_triage.demotion_guard_cli
    python -m omnimarket.nodes.node_linear_triage.demotion_guard_cli --json
    python -m omnimarket.nodes.node_linear_triage.demotion_guard_cli \
        --report-json linear-demotion-guard-report.json

Exit codes:
    0 — the probe is proven live AND every team either carries zero git
        automations or carries only completed-type targets.
    1 — FAIL: at least one automation would demote a verified-Done ticket, or a
        target state was unreadable, or the positive control did not hold
        (enumeration failed, zero teams, a team skipped, a team probe errored,
        or a page came back possibly truncated). All of these fail closed.
    2 — usage/configuration error (missing LINEAR_API_KEY, unwritable report).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_demotion_guard import (
    HandlerGitAutomationDemotionGuard,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_demotion_guard import (
    render_demotion_report,
)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Assert that no Linear git automation resolves a completed-type ticket "
            "to a non-completed state (OMN-16536 AC#2)."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="as_json",
        help="Emit the audit report as JSON on stdout instead of the text summary.",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=None,
        help=(
            "Also write the audit report as JSON to this path. Lets a CI job keep "
            "the readable summary in its log AND upload a machine-readable artifact "
            "from a SINGLE probe — re-running the probe to get the other format "
            "would report on a different moment in time."
        ),
    )
    args = parser.parse_args(argv)

    # Distinguish "not configured" (exit 2, an operator problem) from "ran and
    # found drift" (exit 1). An unarmed guard reporting green would be the exact
    # false assurance this ticket is about, so it is never exit 0.
    if not os.environ.get("LINEAR_API_KEY", ""):
        sys.stderr.write(
            "ERROR: LINEAR_API_KEY is not set. The demotion ratchet cannot read "
            "Linear's git-automation settings, so it can make NO assertion about "
            "whether a verified-Done ticket can currently be silently reverted. "
            "An unarmed guard is not a passing guard — failing closed.\n"
        )
        return 2

    report = HandlerGitAutomationDemotionGuard().handle(now=datetime.now(UTC))

    if args.as_json:
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(render_demotion_report(report) + "\n")

    if args.report_json is not None:
        try:
            args.report_json.write_text(
                report.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write(f"ERROR: could not write --report-json: {exc}\n")
            return 2

    if not report.passed:
        if not report.positive_control_ok:
            sys.stderr.write(
                "\nFAIL: the demotion ratchet could not prove it read the workspace, "
                "so it asserts nothing.\n"
                f"{report.failure_reason}\n"
                "This is deliberate: the ratchet's passing state is an empty "
                "automation set, which is indistinguishable from a revoked token, a "
                "renamed field, or a truncated page. Emptiness is only believed "
                "behind a proven probe.\n"
            )
            return 1
        sys.stderr.write(
            "\nFAIL: a Linear git automation silently reverts verified-Done tickets.\n"
            "A GitAutomationState has no source-state predicate — it fires "
            "unconditionally, so a non-completed target demotes a Done ticket "
            "whenever any PR cites it, including a bare non-closing 'Refs:' mention "
            "in a PR for an unrelated ticket (OMN-16536).\n"
            "Fix: delete the offending mapping via gitAutomationStateDelete, or "
            "retarget it to a completed-type state. Note that gitAutomationStateUpdate "
            "with stateId:null fails server-side (INTERNAL_SERVER_ERROR, verified "
            "2026-08-27) — delete is the working mechanism.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
