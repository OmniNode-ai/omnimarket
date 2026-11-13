# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime command line of node_dod_closeout_sweep_compute's scope bundle (OMN-20655).

    python -m omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep bundle \\
        --candidates C.json --pr-state P.json --text-state T.json --max 12 \\
        [--contract-state K.json] [--overlay overlay.yaml] [--examined-memory PATH] [--no-record]
    python -m omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep prs OMN-1 OMN-2 \\
        [--wait-s 60] [--deadline ISO8601] [--overlay overlay.yaml]

    python -m omnimarket.nodes.node_dod_closeout_sweep_compute.runtime_dod_closeout_sweep zero-flip \\
        --summary RUN.json --stamp 2026-11-08T22:00:00Z [--rows-file ROWS.txt]

``zero-flip`` prints the typed reading of a run that flipped nothing and writes its alarm row;
it exits 0 for a zero-flip run (OMN-20718), 2 only for an unreadable summary.

``prs`` prints the PR row of each ticket ({id, merged, open, error}): merged PRs are read on
each overlay repository's own default branch (every repository the overlay lists),
open PRs from the PR watcher's state; an unread repository is named on stderr and an unread or
stale watcher state is an error on every row.

The bundle ranks tickets by when each was last examined and holds one whose rejection is unchanged
(OMN-23282): the selection reads the examination memory (the overlay's examined_memory_path under
OMNI_HOME, or --examined-memory), and writes it back with each ticket it picked, examined now.
--no-record reads the memory and leaves it as it is, for a probe that is not a closer run.

Without --contract-state the contract index reads contracts/ on each overlay repository's
default branch under OMNI_HOME (the real backend); the handler itself reads nothing. Prints
the bounded bundle as one JSON object, or exits 2 naming what was unreadable; an input the
bundle cannot read is never an empty bundle.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.events.closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)
from omnimarket.events.dod_closeout_sweep import (
    STAMP_PATTERN,
    EnumCloseoutDecisionKind,
    ModelDodCloseoutDecisionRequest,
    ModelRunSummary,
)
from omnimarket.handlers.closeout_boundary_reads import (
    read_text_file,
    registry_root_from_environment,
)
from omnimarket.handlers.closeout_examined_memory import (
    read_examined_memory,
    write_examined_memory,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_completion_rules import (
    zero_flip_reading,
    zero_flip_row,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_contract_index import (
    GitCloneContractIndex,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_pr_sources import (
    GitCloneMergeSubjects,
    wait_for_watcher_state,
    watcher_state_path,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_overlay import (
    load_scope_overlay,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.closer_scope_rules import (
    ScopeInputUnreadableError,
)
from omnimarket.nodes.node_dod_closeout_sweep_compute.handlers.handler_dod_closeout_sweep import (
    HandlerDodCloseoutSweep,
)


def read_array(path: Path, name: str) -> list[dict[str, object]]:
    try:
        text = read_text_file(path)
    except OSError as exc:
        raise ScopeInputUnreadableError(f"{name} {path}: unreadable: {exc}") from exc
    try:
        rows = json.loads(text)
    except ValueError as exc:
        raise ScopeInputUnreadableError(f"{name} {path}: not JSON: {exc}") from exc
    if not isinstance(rows, list):
        raise ScopeInputUnreadableError(
            f"{name} {path}: not a JSON array ({type(rows).__name__})"
        )
    return [r if isinstance(r, dict) else {} for r in rows]


def run_prs(args: argparse.Namespace) -> int:
    """The `prs` command: read the clones and the watcher state, decide through the handler."""
    try:
        overlay = load_scope_overlay(args.overlay)
        home = registry_root_from_environment()
        if not home:
            raise ScopeInputUnreadableError("OMNI_HOME is not set")
        subjects = GitCloneMergeSubjects(Path(home), overlay).subjects()
        state, why = wait_for_watcher_state(
            watcher_state_path(Path(home)),
            overlay,
            wait_s=args.wait_s,
            deadline=args.deadline,
        )
        request = ModelDodCloseoutDecisionRequest(
            kind=EnumCloseoutDecisionKind.PR_ROWS,
            date="1970-01-01",
            pr_tickets=args.tickets,
            pr_subjects=subjects,
            pr_watcher_state=state,
            pr_watcher_error=why,
            scope_overlay=overlay,
        )
        result = HandlerDodCloseoutSweep().handle(request)
    except (ScopeInputUnreadableError, OSError, ValueError) as exc:
        sys.stderr.write(f"closeout-scope: REFUSED: {exc}\n")
        return 2
    if result.unread_repositories:
        sys.stderr.write(
            f"closeout-scope: clones not read: {', '.join(result.unread_repositories)}\n"
        )
    sys.stdout.write(json.dumps(result.pr_rows) + "\n")
    return 0


def run_zero_flip(args: argparse.Namespace) -> int:
    """The `zero-flip` command (OMN-20718): read the run summary, print the typed reading.

    A run that flipped nothing is a working closer: this exits 0 and writes the reading's
    alarm row to --rows-file (nothing when the run flipped a ticket or was a dry run). It exits
    2 only when the summary cannot be read: an unreadable input is never a zero-flip.
    """
    try:
        summary = ModelRunSummary.model_validate_json(read_text_file(args.summary))
        reading = zero_flip_reading(summary, args.stamp)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"closeout-zero-flip: REFUSED: {exc}\n")
        return 2
    if reading is None:
        sys.stdout.write("null\n")
        return 0
    if args.rows_file is not None:
        try:
            args.rows_file.write_text(zero_flip_row(reading) + "\n", encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"closeout-zero-flip: REFUSED: {exc}\n")
            return 2
    sys.stdout.write(reading.model_dump_json() + "\n")
    return 0


def _clock_now() -> str:
    """The instant in the format Linear stamps a comment (millisecond UTC), so the two compare."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _examined_memory_path(overlay: ModelCloseoutScopeOverlay) -> Path:
    home = registry_root_from_environment()
    if not home:
        raise ScopeInputUnreadableError(
            "OMNI_HOME is not set and --examined-memory was not given"
        )
    return Path(home) / overlay.examined_memory_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    bundle = sub.add_parser(
        "bundle", help="the bounded scope the workflow selects from"
    )
    bundle.add_argument("--candidates", type=Path, required=True)
    bundle.add_argument("--pr-state", type=Path, required=True)
    bundle.add_argument("--text-state", type=Path, required=True)
    bundle.add_argument("--max", type=int, required=True, dest="limit")
    bundle.add_argument("--contract-state", type=Path)
    bundle.add_argument("--overlay", type=Path)
    bundle.add_argument("--examined-memory", type=Path)
    bundle.add_argument("--no-record", action="store_true")
    bundle.add_argument(
        "--now", help="the instant of the selection, ISO 8601 UTC; default the clock"
    )
    bundle.add_argument(
        "--extra-clone-root",
        type=Path,
        action="append",
        default=[],
        help="a clone outside the registry root whose contracts/ also counts (repeatable)",
    )
    prs = sub.add_parser("prs", help="merged and open PRs per ticket")
    prs.add_argument("tickets", nargs="+")
    prs.add_argument("--wait-s", type=int, default=60)
    prs.add_argument("--deadline", type=datetime.fromisoformat)
    prs.add_argument("--overlay", type=Path)
    zero = sub.add_parser(
        "zero-flip",
        help="the typed reading and alarm row of a run that closed no ticket",
    )
    zero.add_argument("--summary", type=Path, required=True)
    zero.add_argument(
        "--stamp", required=True, help="ledger stamp YYYY-MM-DDTHH:MM:SSZ"
    )
    zero.add_argument("--rows-file", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "zero-flip":
        if not STAMP_PATTERN.match(args.stamp):
            parser.error("--stamp must be a zero-padded ledger stamp")
        return run_zero_flip(args)
    if args.mode == "prs":
        if args.wait_s < 0:
            parser.error("--wait-s must not be negative")
        if args.deadline is not None and args.deadline.tzinfo is None:
            parser.error("--deadline needs a timezone")
        return run_prs(args)
    if args.limit < 1:
        parser.error("--max must be a positive integer")
    try:
        overlay = load_scope_overlay(args.overlay)
        candidates = read_array(args.candidates, "--candidates")
        if args.contract_state is not None:
            contract_state = read_array(args.contract_state, "--contract-state")
        else:
            home = registry_root_from_environment()
            if not home:
                raise ScopeInputUnreadableError("OMNI_HOME is not set")
            ids = GitCloneContractIndex(
                Path(home), overlay, args.extra_clone_root
            ).contract_ids()
            contract_state = [
                {"id": c["id"], "has_contract": c["id"] in ids}
                for c in candidates
                if isinstance(c.get("id"), str)
            ]
        pr_state = read_array(args.pr_state, "--pr-state")
        if candidates and not pr_state:
            raise ScopeInputUnreadableError(
                f"--pr-state {args.pr_state}: an empty array for {len(candidates)} candidate(s); "
                "the prs command prints a row for every ticket it is given, so the PR reader "
                "failed and every ticket would read as waiting on PR state"
            )
        memory_path = args.examined_memory or _examined_memory_path(overlay)
        examined = read_examined_memory(memory_path)
        now = args.now or _clock_now()
        request = ModelDodCloseoutDecisionRequest(
            kind=EnumCloseoutDecisionKind.SCOPE_BUNDLE,
            date="1970-01-01",
            scope_candidates=candidates,
            scope_pr_state=pr_state,
            scope_text_state=read_array(args.text_state, "--text-state"),
            scope_contract_state=contract_state,
            scope_overlay=overlay,
            max_candidates=args.limit,
            scope_examined=examined,
            scope_now=now,
        )
        result = HandlerDodCloseoutSweep().handle(request)
        if result.scope_examined_next is not None and not args.no_record:
            write_examined_memory(memory_path, dict(result.scope_examined_next))
    except (ScopeInputUnreadableError, OSError, ValueError) as exc:
        sys.stderr.write(f"closeout-scope: REFUSED: {exc}\n")
        return 2
    sys.stdout.write(
        json.dumps(result.scope_bundle, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
