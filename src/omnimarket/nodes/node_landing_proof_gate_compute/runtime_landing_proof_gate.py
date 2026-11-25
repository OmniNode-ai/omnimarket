# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's one command: prints the typed answer (PROVEN or DRAFT, reasons named) as one JSON line.

Exit 0 on PROVEN, 1 on DRAFT. Any failure to read or to decide prints DRAFT with the reason "proof unreadable".
Every input is an argument: the pull request text file, the ledger file, the clone, the time budget.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from omnimarket.nodes.node_landing_proof_gate_compute.adapters.policy import (
    load_policy,
)
from omnimarket.nodes.node_landing_proof_gate_compute.adapters.readers import (
    DeadlinePatchIdReader,
    FileProofReader,
    GitPatchIdReader,
    LedgerFileReader,
)
from omnimarket.nodes.node_landing_proof_gate_compute.handlers.handler_landing_proof_gate import (
    UNREAD,
    HandlerLandingProofGate,
)
from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    EnumProofAnswer,
    EnumProofCode,
    ModelProofGateRequest,
    ModelProofGateResult,
    ModelProofReason,
)

DEFAULT_BUDGET_S = 4.0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="node_landing_proof_gate_compute",
        description="Answer PROVEN or DRAFT for one pull request at one head.",
    )
    p.add_argument("--repo", required=True)
    p.add_argument("--number", required=True, type=int)
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument(
        "--pr-text-file",
        default="",
        help="JSON with body, comments and files of the pull request",
    )
    p.add_argument(
        "--ledger-path",
        default="",
        help="the ledger file; archive files beside it are read too",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help="the caller has no ledger; the answer says it was not checked",
    )
    p.add_argument(
        "--clone",
        default=".",
        help="a clone holding origin/<base> and the commits, for patch ids",
    )
    p.add_argument("--budget-s", type=float, default=DEFAULT_BUDGET_S)
    return p


def _draft(reason: str) -> ModelProofGateResult:
    return ModelProofGateResult(
        answer=EnumProofAnswer.DRAFT,
        reasons=(
            ModelProofReason(
                code=EnumProofCode.PROOF_UNREADABLE, item=reason, detail=UNREAD
            ),
        ),
    )


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(list(argv))
    started = time.monotonic()
    try:
        policy = load_policy()
        git = GitPatchIdReader(args.clone, started + args.budget_s)
        handler = HandlerLandingProofGate(
            policy,
            FileProofReader(args.pr_text_file),
            LedgerFileReader(policy, args.ledger_path),
            DeadlinePatchIdReader(git, args.budget_s),
        )
        result = handler.handle(
            ModelProofGateRequest(
                repo=args.repo,
                number=args.number,
                base=args.base,
                head=args.head,
                check_ledger=not args.no_ledger,
            )
        )
    except Exception as exc:  # fail closed: whatever broke, the answer is DRAFT
        result = _draft(f"{type(exc).__name__}")
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0 if result.admitted else 1
