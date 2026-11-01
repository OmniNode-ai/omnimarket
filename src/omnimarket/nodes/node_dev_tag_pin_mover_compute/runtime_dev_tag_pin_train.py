# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI for one night of the dev-tag pin train (OMN-21895).

Usage:
    python -m omnimarket.nodes.node_dev_tag_pin_mover_compute.runtime_dev_tag_pin_train \
        --repo-root . --registry-root .clones --green-from github \
        [--apply | --publish] --plan-out plan.json --body-out body.md

Without ``--apply`` or ``--publish`` this is a dry run: the plan prints and no
file is written. ``--apply`` writes the resolved pyproject.toml and uv.lock.
``--publish`` also pushes the one fixed branch and opens or updates its PR; it
runs only after the candidate locked in a scratch copy. A night with no
co-resolving tag set, or whose candidate does not lock, prints the conflicting
URLs and a SKIPPED row and exits 0 with nothing pushed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from omnimarket.nodes.node_dev_tag_pin_mover_compute.handlers.handler_dev_tag_pin_train import (
    HandlerDevTagPinTrain,
    ModelTrainPorts,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_train import (
    EnumTrainStatus,
    ModelTrainReport,
)
from omnimarket.nodes.node_dev_tag_pin_mover_compute.train_sources import (
    assume_unread,
    github_check_runs_green,
    make_gather,
    make_publish,
    make_scratch_resolve,
)

BRANCH = "bot/omn-21895-sibling-pin-train"
TITLE = "chore(deps, OMN-21895): nightly sibling pin train [bot]"
COMMIT_MESSAGE = "chore(deps, OMN-21895): nightly sibling pin train [bot]"


def _echo(report: ModelTrainReport) -> None:
    out = sys.stdout
    out.write(report.model_dump_json(indent=2) + "\n")
    if report.skipped_row:
        out.write(report.skipped_row + "\n")
    for move in report.selection.moves:
        out.write(f"[train] {move.distribution}: {move.old_tag} -> {move.new_tag}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--green-from", choices=["github", "none"], default="github")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--plan-out", type=Path)
    parser.add_argument("--body-out", type=Path)
    parser.add_argument("--branch", default=BRANCH)
    parser.add_argument(
        "--run-url", default="", help="the workflow run, for the commit trailer"
    )
    parser.add_argument("--base", default="dev")
    parser.add_argument("--workflow-name", default="sibling-pin-train")
    args = parser.parse_args(argv)

    root: Path = args.repo_root
    oracle = github_check_runs_green if args.green_from == "github" else assume_unread
    ports = ModelTrainPorts(
        gather=make_gather(
            consumer=root.resolve().name,
            registry_root=args.registry_root,
            is_green=oracle,
        ),
        resolve=make_scratch_resolve(root),
        publish=make_publish(
            root,
            branch=args.branch,
            base=args.base,
            title=TITLE,
            commit_message=COMMIT_MESSAGE,
            trailers=(
                f"Onex-Workflow: {args.workflow_name}",
                f"Onex-Run: {args.run_url}",
            ),
        ),
    )
    pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    lock_text = (root / "uv.lock").read_text(encoding="utf-8")
    try:
        report, new_pyproject, new_lock = HandlerDevTagPinTrain().handle(
            pyproject_text=pyproject_text,
            lock_text=lock_text,
            ports=ports,
            publish_enabled=args.publish,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 1
    _echo(report)
    if args.plan_out:
        args.plan_out.write_text(
            report.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
    if args.body_out:
        args.body_out.write_text(report.body, encoding="utf-8")
    if args.apply and report.status is EnumTrainStatus.DRY_RUN:
        (root / "pyproject.toml").write_text(new_pyproject, encoding="utf-8")
        (root / "uv.lock").write_text(new_lock, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
