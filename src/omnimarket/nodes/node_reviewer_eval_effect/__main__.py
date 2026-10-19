# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Entry point of the hostile reviewer eval (OMN-17427).

    python -m omnimarket.nodes.node_reviewer_eval_effect \
        --reviewer-ref REF [--cases FILE] [--prior-cases FILE] [--observed-dir DIR]

Scores the reviewer against the eval set: recall on true catches and the
false-block rate on clean heads. With ``--observed-dir`` each case's verdict is
read from ``DIR/<case_id>.json``, a cli_review result of a rerun; a missing file
is a case with no verdict. Without it the verdicts are the ones the gate
recorded. ``--prior-cases`` is the base branch's copy of the set, which a change
may grow but never shrink, relabel or score lower. The scored event is published
on the contract's terminal topic and printed to stdout.

Exit 0 when the score holds the baseline, 1 when it regressed, 2 when an input
could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_reviewer_eval_effect.handlers.adapter_publisher import (
    publish_scored,
)
from omnimarket.nodes.node_reviewer_eval_effect.handlers.handler_reviewer_eval import (
    HandlerReviewerEval,
    verdict_from_review_result,
)
from omnimarket.nodes.node_reviewer_eval_effect.models import (
    ModelReviewerEvalCaseSet,
    ModelReviewerEvalObservation,
    ModelReviewerEvalRequest,
)

DEFAULT_CASES = Path(__file__).parent / "eval_cases.yaml"


def load_case_set(path: Path) -> ModelReviewerEvalCaseSet:
    return ModelReviewerEvalCaseSet.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--reviewer-ref", required=True)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--prior-cases", type=Path)
    parser.add_argument("--observed-dir", type=Path)
    parser.add_argument("--correlation-id", default=None)
    args = parser.parse_args(argv)

    try:
        case_set = load_case_set(args.cases)
        prior = None
        if args.prior_cases is not None:
            if args.prior_cases.is_file():
                prior = load_case_set(args.prior_cases)
            else:
                sys.stderr.write(
                    f"reviewer eval: no prior set at {args.prior_cases}; "
                    "the ratchet starts with this one\n"
                )
        if args.observed_dir is None:
            source = "recorded"
            observations = tuple(
                ModelReviewerEvalObservation(
                    case_id=case.case_id, verdict=case.recorded_verdict
                )
                for case in case_set.cases
            )
        else:
            source = "live"
            found = []
            for case in case_set.cases:
                result_file = args.observed_dir / f"{case.case_id}.json"
                if result_file.is_file():
                    document = json.loads(result_file.read_text(encoding="utf-8"))
                    found.append(
                        ModelReviewerEvalObservation(
                            case_id=case.case_id,
                            verdict=verdict_from_review_result(document),
                        )
                    )
            observations = tuple(found)
        request = ModelReviewerEvalRequest(
            correlation_id=args.correlation_id or str(uuid.uuid4()),
            reviewer_ref=args.reviewer_ref,
            source=source,
            case_set=case_set,
            observations=observations,
            prior_case_set=prior,
        )
    except (OSError, ValueError, ValidationError, yaml.YAMLError) as exc:
        sys.stderr.write(f"reviewer eval: input unreadable: {exc}\n")
        return 2

    scored = HandlerReviewerEval().handle(request)
    sys.stdout.write(scored.model_dump_json(indent=2) + "\n")
    publish_scored(scored)
    sys.stderr.write(
        f"reviewer eval ({scored.source}, {scored.reviewer_ref}): recall "
        f"{scored.caught}/{scored.true_catch_cases} = {scored.recall:.4f} "
        f"(baseline {scored.baseline.recall:.4f}); false-block rate "
        f"{scored.false_blocks}/{scored.clean_cases} = {scored.false_block_rate:.4f} "
        f"(baseline {scored.baseline.false_block_rate:.4f})\n"
    )
    for reason in scored.reasons:
        sys.stderr.write(f"reviewer eval: REGRESSION: {reason}\n")
    return 1 if scored.regressed else 0


if __name__ == "__main__":
    sys.exit(main())
