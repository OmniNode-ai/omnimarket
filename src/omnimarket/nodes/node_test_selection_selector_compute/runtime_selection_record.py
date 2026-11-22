# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The workflow's entry to build_selection_record: reads the selections ``detect_test_paths`` wrote,
builds the record through the handler and writes it as JSON. All file I/O of the operation is here."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from omnimarket.handlers.adapter_test_selection_policy_file import (
    load_policy,
    policy_version,
    selector_version,
)
from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_selection_record import (
    HandlerSelectionRecord,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_file_level_closure import (
    EnumRootConftestEdge,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumSelectionKind,
    ModelSelectionRecordRequest,
    ModelTestSelection,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a pull request's selection record"
    )
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr-number", type=int, default=None)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--checkout-sha", required=True)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--run-attempt", type=int, required=True)
    parser.add_argument("--changed-files-from", type=Path, required=True)
    parser.add_argument("--feature-flag", choices=("on", "off"), required=True)
    parser.add_argument(
        "--stage-selection",
        action="append",
        default=[],
        metavar="STAGE=FILE",
        help="the selection of one stage, as detect_test_paths wrote it; repeat per stage",
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--github-output", type=Path, default=None)
    args = parser.parse_args(argv)

    source = SourceTextFile()
    stages: dict[int, ModelTestSelection] = {}
    for item in args.stage_selection:
        stage, _, path = item.partition("=")
        stages[int(stage)] = ModelTestSelection.model_validate_json(
            source.read_text(Path(path))
        )
    request = ModelSelectionRecordRequest(
        repo=args.repo,
        pr_number=args.pr_number,
        head_sha=args.head_sha,
        base_sha=args.base_sha,
        checkout_sha=args.checkout_sha,
        run_id=args.run_id,
        run_attempt=args.run_attempt,
        changed_files=tuple(
            line.strip()
            for line in source.read_text(args.changed_files_from).splitlines()
            if line.strip()
        ),
        flag_enabled=args.feature_flag == "on",
        stage_selections=stages,
        repo_root=str(args.repo_root),
        selector_version=selector_version(),
        policy_version=policy_version(),
    )
    policy = load_policy()
    record = HandlerSelectionRecord(None, policy).handle(request)
    args.out.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"retention_days={policy.record_retention_days}\n")
            # The whole-tree collection job runs once, and only when the cut root conftest edge
            # and a narrowed selection under the closure's stage make it the check for the tree.
            closure_stage, root_conftest_edge = policy.closure_settings()
            gate = (
                policy.selection_stage >= closure_stage
                and root_conftest_edge is EnumRootConftestEdge.CUT
                and record.executed_selection.kind is EnumSelectionKind.NARROWED
            )
            handle.write(f"collection_gate={str(gate).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
