# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Change-aware test path resolution for omnimarket CI."""

from __future__ import annotations

import argparse
import sys
from fnmatch import fnmatchcase
from pathlib import Path

from omnimarket.handlers.adapter_test_selection_policy_file import load_policy
from omnimarket.nodes.node_test_selection_selector_compute.adapters.adjacency_file import (
    load_adjacency_map,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_file_level_closure import (
    closure_selection,
    legacy_module_escalation,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_path_resolution import (
    resolve_changed_files,
)
from omnimarket.nodes.node_test_selection_selector_compute.handlers.handler_test_selection_selector import (
    HandlerTestSelectionSelector,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_record import (
    EnumFullSuiteReason,
    ModelTestSelection,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorRequest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

TEST_PREFIX = "tests/"

FULL_SUITE_BRANCHES = {"main"}


def resolve_test_paths(
    changed_files: list[str],
    adjacency_path: Path,
    repo_root: Path | None = None,
) -> list[str]:
    """Map changed file paths to deterministic test directories.

    Behavior (the module-level resolver; the file-level closure is in compute_selection):
      - Source changes under src/omnimarket/<module>: include tests/<module>/,
        except for a node-scoped module, where the import graph narrows it (OMN-23719).
      - Test-only changes under tests/: include the changed test directory; a
        root-level tests/*.py file selects itself and its importers (OMN-26272).
      - Files outside src/ and tests/ contribute nothing; paths not on disk are dropped.
    """
    config = load_adjacency_map(adjacency_path)
    result = resolve_changed_files(changed_files, config, repo_root or REPO_ROOT)
    return list(result.selected_paths)


def compute_selection(
    changed_files: list[str],
    adjacency_path: Path,
    ref_name: str,
    event_name: str = "pull_request",
    feature_flag_enabled: bool = True,
    repo_root: Path | None = None,
    base_ref: str | None = None,
    stage: int | None = None,
) -> ModelTestSelection:
    config = load_adjacency_map(adjacency_path)
    policy = load_policy()
    closure_stage, root_conftest_edge = policy.closure_settings()
    closure = (stage if stage is not None else policy.selection_stage) >= closure_stage
    root = repo_root or REPO_ROOT

    if not feature_flag_enabled:
        return _full_suite(EnumFullSuiteReason.FEATURE_FLAG_OFF)

    if ref_name in FULL_SUITE_BRANCHES:
        return _full_suite(EnumFullSuiteReason.MAIN_BRANCH)
    if event_name == "merge_group":
        return _full_suite(EnumFullSuiteReason.MERGE_GROUP)
    if event_name == "schedule":
        return _full_suite(EnumFullSuiteReason.SCHEDULED)

    if any(config.is_test_infrastructure(changed) for changed in changed_files):
        return _full_suite(EnumFullSuiteReason.TEST_INFRASTRUCTURE)

    # OMN-20180: delegation coverage spans unit, golden and flat tests, so
    # module-to-directory narrowing cannot safely represent these surfaces.
    if any(
        fnmatchcase(changed, pattern)
        for changed in changed_files
        for pattern in config.full_suite_path_globs
    ):
        return _full_suite(EnumFullSuiteReason.PROTECTED_SURFACE)

    # OMN-26277: at the closure's stage the file-level closure replaces the module-level escalations.
    # Below it, the module-level escalations run, then the diff resolves with a declared data file's
    # changed keys read against base_ref (OMN-26275).
    if closure:
        resolved = closure_selection(changed_files, config, root, root_conftest_edge)
        if isinstance(resolved, EnumFullSuiteReason):
            return _full_suite(resolved)
    elif reason := legacy_module_escalation(changed_files, config):
        return _full_suite(reason)
    else:
        resolved = resolve_changed_files(changed_files, config, root, base_ref)
        if resolved.full_suite_reason is not None:
            return _full_suite(resolved.full_suite_reason)
    if not (selected := list(resolved.selected_paths)) and not resolved.data_only:
        # OMN-20305: nothing narrowed, so the whole tests/ tree runs. Take the
        # full-suite shard matrix; an is_full_suite=False selection of tests/
        # became one serial Split 1/1 job (63 minutes on omnimarket#3175).
        return _full_suite(EnumFullSuiteReason.NO_NARROWABLE_SELECTION)
    # OMN-15639: repo-wide gates are unioned in after narrowing. They assert
    # invariants over files the adjacency map cannot attribute to a single
    # module (all of src/**/contract.yaml, for instance), so a narrowed run
    # that omits them would let the invariant be reintroduced on the everyday
    # dev path. Not filtered against the on-disk tree -- see the field
    # docstring on ModelAdjacencyMap.always_selected_paths.
    if not any(path == TEST_PREFIX for path in selected):
        selected = sorted(set(selected) | config.narrowed_extra_paths(changed_files))
    # OMN-23296: shard count and weight bound come from estimated test time.
    decision = HandlerTestSelectionSelector(None, load_policy()).handle(
        ModelSelectorRequest(selected_paths=tuple(selected), repo_root=str(root))
    )
    if decision.split_count is None:
        return _full_suite(EnumFullSuiteReason.NARROWED_SELECTION_TOO_HEAVY)

    return ModelTestSelection(
        selected_paths=selected,
        split_count=decision.split_count,
        is_full_suite=False,
        full_suite_reason=None,
        matrix=list(range(1, decision.split_count + 1)),
    )


def _full_suite(reason: EnumFullSuiteReason) -> ModelTestSelection:
    return ModelTestSelection.full_suite(reason, load_policy().full_split_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve change-aware test paths")
    parser.add_argument(
        "--changed-files-from",
        type=Path,
        required=True,
        help="Path to a file with one changed-file path per line.",
    )
    parser.add_argument("--ref-name", required=True)
    parser.add_argument("--stage", type=int, default=None)
    parser.add_argument("--event-name", default="pull_request")
    parser.add_argument(
        "--adjacency",
        type=Path,
        default=Path(__file__).parent / "test_selection_adjacency.yaml",
    )
    parser.add_argument("--base-ref", default=None)
    parser.add_argument(
        "--feature-flag",
        choices=("on", "off"),
        default="on",
    )
    args = parser.parse_args(argv)

    changed = [
        line.strip()
        for line in args.changed_files_from.read_text().splitlines()
        if line.strip()
    ]
    selection = compute_selection(
        changed_files=changed,
        adjacency_path=args.adjacency,
        ref_name=args.ref_name,
        event_name=args.event_name,
        feature_flag_enabled=(args.feature_flag == "on"),
        base_ref=args.base_ref,
        stage=args.stage,
    )
    sys.stdout.write(selection.model_dump_json())
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
