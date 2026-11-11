# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_proof_judge_compute <verb>``.

The pool driver's own command (OMN-23642). Each answering verb reads ONE JSON request from stdin, loads the deployment
overlay, and prints the result model as one JSON object on stdout:

``changed-path-live``  the declared not-applicable class for the changed-path-live rule; the substitute receipts at the
                       request's head are read from the store the overlay names.
``select-tests``       the repository's own marker selection, as the pool applies it.
``focused-tests``      the focused-tests verdict with every not-run test named.
``retention``          the instant the proof's log may be removed.
``record-substitute``  write one substitute receipt (a node's own run on a lab host, bound to a head).

Exit codes: 0 answered; 3 a deployment fact is missing (INDETERMINATE: ... on stderr).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime

from pydantic import BaseModel, ValidationError

from omnimarket.adapters.lab_proof_receipt_store import (
    DirectoryReceiptStore,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_changed_path_live import (
    HandlerChangedPathLive,
)
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_focused_tests import (
    HandlerFocusedTests,
)
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_log_retention import (
    HandlerLogRetention,
)
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_test_selection import (
    HandlerTestSelection,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_changed_path_live import (
    ModelChangedPathLiveRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_focused_tests import (
    ModelFocusedTestsRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_log_retention import (
    ModelLogRetentionRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_substitute_receipt import (
    ModelSubstituteReceipt,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_test_selection import (
    ModelTestSelectionRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    NODE_NAME,
    ModelLabProofJudgeOverlay,
    load_overlay,
)


def _store(overlay: ModelLabProofJudgeOverlay) -> DirectoryReceiptStore:
    return DirectoryReceiptStore.from_env(
        overlay.substitute_receipts.root_env, overlay.substitute_receipts.subdir
    )


def _answer(result: BaseModel) -> int:
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


def _changed_path_live(overlay: ModelLabProofJudgeOverlay) -> int:
    request = ModelChangedPathLiveRequest.model_validate_json(sys.stdin.read())
    receipts = _store(overlay).read_for_head(request.repo, request.head)
    request = request.model_copy(update={"substitute_receipts": receipts})
    return _answer(HandlerChangedPathLive(overlay).handle(request))


def _record_substitute(
    overlay: ModelLabProofJudgeOverlay, args: argparse.Namespace
) -> int:
    receipt = ModelSubstituteReceipt(
        receipt_id=f"{args.node}-{args.head[:12]}-{args.host}",
        repo=args.repo,
        node=args.node,
        head=args.head,
        host=args.host,
        host_class=args.host_class,
        command=args.command,
        exit_code=args.exit_code,
        finished_at=datetime.fromisoformat(args.finished_at),
        log_path=args.log_file,
    )
    sys.stdout.write(_store(overlay).write(receipt) + "\n")
    return 0


def _run(args: argparse.Namespace) -> int:
    overlay = load_overlay()
    if args.verb == "changed-path-live":
        return _changed_path_live(overlay)
    if args.verb == "select-tests":
        return _answer(
            HandlerTestSelection(overlay).handle(
                ModelTestSelectionRequest.model_validate_json(sys.stdin.read())
            )
        )
    if args.verb == "focused-tests":
        return _answer(
            HandlerFocusedTests(overlay).handle(
                ModelFocusedTestsRequest.model_validate_json(sys.stdin.read())
            )
        )
    if args.verb == "retention":
        return _answer(
            HandlerLogRetention(overlay).handle(
                ModelLogRetentionRequest.model_validate_json(sys.stdin.read())
            )
        )
    return _record_substitute(overlay, args)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=NODE_NAME, description=__doc__)
    sub = parser.add_subparsers(dest="verb", required=True)
    for verb in ("changed-path-live", "select-tests", "focused-tests", "retention"):
        sub.add_parser(verb)
    record = sub.add_parser("record-substitute")
    for flag in (
        "repo",
        "node",
        "head",
        "host",
        "host-class",
        "command",
        "log-file",
        "finished-at",
    ):
        record.add_argument(f"--{flag}", required=True)
    record.add_argument("--exit-code", type=int, required=True)
    try:
        return _run(parser.parse_args(list(sys.argv[1:] if argv is None else argv)))
    except (NodeOverlayError, ValidationError, ValueError, OSError) as exc:
        sys.stderr.write(f"INDETERMINATE: {type(exc).__name__}: {exc}\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
