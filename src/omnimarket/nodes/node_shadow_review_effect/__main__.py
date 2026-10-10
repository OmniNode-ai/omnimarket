# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_shadow_review_effect (OMN-20422).

Usage (one tick, as the lab timer runs it):
    python -m omnimarket.nodes.node_shadow_review_effect \
        --watcher-state <pr-watcher-state.json> --store <store dir> \
        [--window-start <T1>] [--only <repo#n> ...] [--dry-run]

Builds one ModelShadowReviewRequest from the watcher state, runs the handler
in process on this lab host (Codex and the GLM harness are host tools, so the
tick cannot run inside a runtime container or a CI runner), and writes the
ModelShadowReviewResult as JSON on stdout. With no window start the tick
reviews nothing unless --only names PRs. Exit 0 when the tick ran, 2 on bad
input.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from omnimarket.nodes.node_shadow_review_effect.handlers.handler_shadow_review import (
    HandlerShadowReview,
)
from omnimarket.nodes.node_shadow_review_effect.handlers.shadow_effects import (
    CanonicalCloneDiffSource,
    LabShadowReviewer,
)
from omnimarket.nodes.node_shadow_review_effect.handlers.source_watcher_state import (
    candidates_from_watcher_state,
)
from omnimarket.nodes.node_shadow_review_effect.models.model_shadow_review import (
    ModelShadowReviewPolicy,
    ModelShadowReviewRequest,
)

_CI_MARKERS = ("GITHUB_ACTIONS", "CI")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="node_shadow_review_effect")
    p.add_argument("--watcher-state", type=Path, required=True)
    p.add_argument("--store", type=Path, required=True)
    p.add_argument(
        "--window-start", default=os.environ.get("SHADOW_REVIEW_WINDOW_START") or None
    )
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--max-reviews", type=int, default=2)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--omni-home", type=Path, default=None)
    p.add_argument(
        "--harness-script",
        type=Path,
        default=os.environ.get("SHADOW_REVIEW_HARNESS_SCRIPT"),
        help="harness_delegate.py of the omni plugin on this host",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    # Codex stays out of CI (operator ruling 2026-10-09T04:15:56Z): the tick
    # runs on a lab host, never inside a CI runner where PR code runs.
    ci_markers = [v for v in _CI_MARKERS if os.environ.get(v)]
    if ci_markers:
        sys.stderr.write(
            f"refused: a shadow-review tick never runs in CI ({ci_markers[0]} is set)\n"
        )
        return 2
    if not args.watcher_state.is_file():
        sys.stderr.write(f"watcher state not found: {args.watcher_state}\n")
        return 2
    if not args.dry_run and (
        args.harness_script is None or not Path(args.harness_script).is_file()
    ):
        sys.stderr.write(
            "--harness-script (or SHADOW_REVIEW_HARNESS_SCRIPT) must name"
            " harness_delegate.py\n"
        )
        return 2
    home = args.omni_home or os.environ.get("OMNI_HOME")
    if not home:
        sys.stderr.write("--omni-home or OMNI_HOME must name the registry root\n")
        return 2
    omni_home = Path(home).resolve()
    policy = ModelShadowReviewPolicy(
        window_start=args.window_start, max_reviews_per_tick=args.max_reviews
    )
    request = ModelShadowReviewRequest(
        candidates=candidates_from_watcher_state(args.watcher_state),
        policy=policy,
        store_root=args.store.resolve(),
        only=tuple(args.only),
        dry_run=args.dry_run,
    )
    handler = HandlerShadowReview(
        diff_source=CanonicalCloneDiffSource(
            omni_home,
            omni_home / "omniclaude/plugins/onex/hooks/lib/canonical_clone_sync.py",
        ),
        reviewer=LabShadowReviewer(
            omniintelligence_dir=omni_home / "omniintelligence",
            venv_dir=args.store.resolve() / ".venv-omniintelligence",
            harness_script=Path(args.harness_script or "/nonexistent"),
            codex_timeout_s=policy.codex_timeout_s,
            glm_call_budget_s=policy.glm_call_budget_s,
        ),
    )
    result = handler.handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
