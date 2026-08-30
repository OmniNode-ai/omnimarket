# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Headless runner for the Linear git-automation drift guard (OMN-15373).

Runs with no human in the loop: probe Linear, assert that no team's git
automation resolves to a ``completed``-type workflow state, print the report,
and exit non-zero on drift. The scheduled runner is
``omnibase_infra/.github/workflows/linear-done-guard-armed.yml`` (OMN-17182),
which holds ``LINEAR_API_KEY`` and checks this repo out at ``dev``; this repo's
own ``.github/workflows/linear-done-guard.yml`` is ``workflow_dispatch``-only
because omnimarket has no Linear credential. Either way the assertion actually
fires rather than sitting in a doc.

Requires:
  LINEAR_API_KEY — Linear API key with read access to team settings.

Usage:
    python -m omnimarket.nodes.node_linear_triage.guard_cli
    python -m omnimarket.nodes.node_linear_triage.guard_cli --json
    python -m omnimarket.nodes.node_linear_triage.guard_cli \
        --exceptions config/linear_git_automation_exceptions.yaml

Exit codes:
    0 — every git automation resolves to a non-completed state (or a live,
        unexpired accepted exception).
    1 — DRIFT: at least one automation mints Done with zero proof, or a target
        state was unreadable, or the probe returned nothing / failed. All of
        these fail closed.
    2 — usage/configuration error (missing key, unreadable exceptions file).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_linear_triage.handlers.handler_git_automation_guard import (
    HandlerGitAutomationGuard,
)
from omnimarket.nodes.node_linear_triage.models.model_git_automation_guard import (
    ModelGitAutomationException,
)
from omnimarket.nodes.node_linear_triage.services.git_automation_guard import (
    render_report,
)

_log = logging.getLogger(__name__)


def load_exceptions(path: Path) -> list[ModelGitAutomationException]:
    """Load the accepted-exception registry.

    Fail-closed on a malformed registry: a file that cannot be parsed, or an
    entry missing a required field, raises rather than being skipped. A silently
    ignored exceptions file would let a typo read as "no exceptions" — benign
    here, but the same tolerance would let a typo read as "suppressed" if the
    polarity were ever inverted. Refuse to guess.
    """
    if not path.exists():
        raise FileNotFoundError(f"exceptions registry not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"exceptions registry must be a mapping: {path}")
    entries = raw.get("exceptions") or []
    if not isinstance(entries, list):
        raise ValueError(f"'exceptions' must be a list in {path}")
    return [ModelGitAutomationException(**entry) for entry in entries]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    parser = argparse.ArgumentParser(
        description=(
            "Assert that no Linear git automation resolves to a completed-type "
            "workflow state (OMN-15373)."
        )
    )
    parser.add_argument(
        "--exceptions",
        type=Path,
        default=None,
        help=(
            "Path to the accepted-exception registry YAML. Omit to run with no "
            "exceptions at all (the strictest form)."
        ),
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
            "the readable summary in its log AND upload a machine-readable "
            "artifact from a SINGLE probe — re-running the probe to get the other "
            "format would report on a different moment in time."
        ),
    )
    args = parser.parse_args(argv)

    exceptions: list[ModelGitAutomationException] = []
    if args.exceptions is not None:
        try:
            exceptions = load_exceptions(args.exceptions)
        except (OSError, ValueError, yaml.YAMLError, ValidationError) as exc:
            sys.stderr.write(f"ERROR: could not load exceptions registry: {exc}\n")
            return 2

    report = HandlerGitAutomationGuard().handle(
        now=datetime.now(UTC), exceptions=exceptions
    )

    if args.as_json:
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(render_report(report) + "\n")

    if args.report_json is not None:
        try:
            args.report_json.write_text(
                report.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            sys.stderr.write(f"ERROR: could not write --report-json: {exc}\n")
            return 2

    if not report.passed:
        sys.stderr.write(
            "\nFAIL: a Linear git automation can mint Done with zero proof.\n"
            "Done is reachable only via dod_verify with durable evidence — a PR "
            "merge is code-only/receipt-bound at best.\n"
            "Fix: retarget the offending automation to a non-completed state "
            "(e.g. 'In Review') via gitAutomationStateUpdate, or register a dated, "
            "owned exception in the registry.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
