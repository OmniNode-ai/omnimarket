# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point of the review finding veto and policy (OMN-17427).

    python -m omnimarket.nodes.node_review_finding_veto_effect \
        --result-file FILE --head-sha SHA [--repo-root DIR] \
        [--base-sha SHA [--prior-state-file FILE] [--report-out FILE]]

Reads the hostile reviewer's result document, verifies each blocking finding that
claims a Python, YAML or JSON file is invalid by parsing that file at ``SHA`` in
the checkout at ``DIR``, and rewrites FILE without the findings the parse
disproves. With ``--base-sha`` it then keeps blocking only the Important findings
the pull request introduced (see the policy handler), and writes the round state
for the next run, the findings that still block and each dropped finding to
``--report-out``. Each dropped finding is printed to stderr with its reason.

Exit 0 when the document was read (rewritten only if something was dropped), 2
when it could not be read or written; the caller then keeps the reviewer's
verdict as it stands.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_diff import (
    GitDiffReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.adapter_git_head import (
    GitHeadFileReader,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.handler_review_finding_policy import (
    HandlerReviewFindingPolicy,
)
from omnimarket.nodes.node_review_finding_veto_effect.handlers.handler_review_finding_veto import (
    HandlerReviewFindingVeto,
)
from omnimarket.nodes.node_review_finding_veto_effect.models import (
    ModelReviewFindingPolicyRequest,
    ModelReviewFindingVetoRequest,
    ModelReviewRoundState,
)


def _prior_state(path: str | None) -> ModelReviewRoundState | None:
    """The previous run's state; None when there is none or it cannot be trusted."""
    if path is None or not Path(path).is_file():
        return None
    try:
        return ModelReviewRoundState.model_validate_json(
            Path(path).read_text(encoding="utf-8")
        )
    except ValueError as exc:
        sys.stderr.write(f"review finding policy: prior state ignored: {exc!r}" + "\n")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--result-file", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--base-sha")
    parser.add_argument("--prior-state-file")
    parser.add_argument("--report-out")
    args = parser.parse_args(argv)

    path = Path(args.result_file)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        reader = GitHeadFileReader(args.repo_root)
        outcome = HandlerReviewFindingVeto(reader).handle(
            ModelReviewFindingVetoRequest(result=document, head_sha=args.head_sha)
        )
        for line in outcome.kept_unverified:
            sys.stderr.write(
                f"review finding veto: syntax claim stays blocking: {line}" + "\n"
            )
        for veto in outcome.vetoed:
            sys.stderr.write(
                f"review finding veto: dropped blocking finding {veto.file_path}:"
                f"{veto.line_start}: {veto.reason}" + "\n"
            )
        result = outcome.result
        changed = bool(outcome.vetoed)
        if args.base_sha:
            policy = HandlerReviewFindingPolicy(
                reader, GitDiffReader(args.repo_root)
            ).handle(
                ModelReviewFindingPolicyRequest(
                    result=result,
                    head_sha=args.head_sha,
                    base_sha=args.base_sha,
                    prior_state=_prior_state(args.prior_state_file),
                )
            )
            for drop in policy.dropped:
                sys.stderr.write(
                    f"review finding policy: dropped blocking finding "
                    f"({drop.disposition}): {drop.reason}" + "\n"
                )
            result = policy.result
            changed = changed or bool(policy.dropped)
            if args.report_out:
                Path(args.report_out).write_text(
                    json.dumps(
                        {
                            "state": policy.state.model_dump(mode="json")
                            if policy.state
                            else None,
                            "kept": list(policy.kept_messages),
                            "dropped": [
                                {"disposition": str(d.disposition), "reason": d.reason}
                                for d in policy.dropped
                            ],
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        if changed:
            path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        sys.stderr.write(f"review finding veto: not applied: {exc!r}" + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
