# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point of the deployment-fact gate (OMN-20287).

    uv run python -m omnimarket.nodes.node_deployment_fact_gate_effect [--base REF | --no-base] [--inventory]

The ``deployment-fact-gate`` pre-commit hook runs it with the default base
(``HEAD``), and the ``Deployment Fact Gate`` CI job with the pull request's
merge base. ``--no-base`` judges the tree against no baseline and so lists
every deployment fact the packaged configs still carry; ``--inventory`` prints
that list on any run.

Exit 0 when nothing new is found, 1 when a new deployment fact or undeclared
key is found, 2 when the configs or the base revision cannot be read.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_deployment_fact_gate_effect.handlers.handler_deployment_fact_gate import (
    DeploymentFactGateError,
    HandlerDeploymentFactGate,
)
from omnimarket.nodes.node_deployment_fact_gate_effect.models.model_deployment_fact_gate import (
    ModelDeploymentFactGateRequest,
    ModelDeploymentFactGateResult,
)

_REMEDY = (
    "Routing decisions belong in a deployment overlay, not in the config this "
    "package ships (operator rulings 2026-09-26T14:31:29Z and 15:24:56Z). Move the "
    "value to the overlay, or use a value in NEUTRAL_LOCAL_ONLY_DEFAULTS "
    "(omnimarket.models.delegation.model_deployment_fact_marker). A new key must be "
    "declared on the file's typed model, and marked with deployment_fact() when it "
    "records a deployment's choice. The base revision is the baseline; it only shrinks."
)


def _render(result: ModelDeploymentFactGateResult, inventory: bool) -> str:
    lines: list[str] = []
    if inventory:
        lines.append(f"deployment facts in packaged configs: {len(result.inventory)}")
        for fact in result.inventory:
            lines.append(
                f"  {fact.file_name} {fact.path} [{fact.kind.value}] {fact.value}"
            )
    if result.new_facts:
        lines.append(
            f"new deployment fact(s) against base {result.base_ref}: "
            f"{len(result.new_facts)}"
        )
        for finding in result.new_facts:
            lines.append(
                f"  {finding.file_name} {finding.path} [{finding.kind.value}] "
                f"{finding.value} ({finding.base_count} -> {finding.head_count})"
            )
    if result.new_undeclared_keys:
        lines.append(
            f"new undeclared key(s) against base {result.base_ref}: "
            f"{len(result.new_undeclared_keys)}"
        )
        for key in result.new_undeclared_keys:
            lines.append(f"  {key.file_name} {key.path}")
    if not result.passed:
        lines.append(_REMEDY)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_deployment_fact_gate_effect")
    base = parser.add_mutually_exclusive_group()
    base.add_argument("--base", default="HEAD", help="baseline revision (default HEAD)")
    base.add_argument(
        "--no-base", action="store_true", help="judge against no baseline"
    )
    parser.add_argument(
        "--inventory", action="store_true", help="print every deployment fact"
    )
    parser.add_argument("--repo-root", default=".", help="repository checkout")
    args = parser.parse_args(argv)
    request = ModelDeploymentFactGateRequest(
        repo_root=str(Path(args.repo_root).resolve()),
        base_ref=None if args.no_base else args.base,
    )
    try:
        result = HandlerDeploymentFactGate().handle(request)
    except DeploymentFactGateError as exc:
        sys.stderr.write(f"deployment-fact gate cannot judge: {exc}\n")
        return 2
    report = _render(result, args.inventory)
    if report:
        (sys.stdout if result.passed else sys.stderr).write(report + "\n")
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
