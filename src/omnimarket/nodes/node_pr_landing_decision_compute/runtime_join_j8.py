# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_pr_landing_decision_compute.runtime_join_j8`` (OMN-23087).

``run``    take one real head from the PR watcher's state, write piece 8's chain and piece 9's answer to the emission
           journal under one correlation id, and print the result as one JSON object.
``tick``   (the default, what the host unit runs: ``--omni-home ROOT --state-dir DIR``) one ``run`` for the golden end and one
           for the error end of the join, then ``DIR/last-run.json`` as the unit's evidence.
``--want golden|error|any``  which end of the join a ``run`` exercises (default any).
``--pr <owner/repo#n>``      pin the head instead of picking one.
``--inject <break>``         swap in a named injected break (the failing control); refused unless the overlay allows it and names a
                           control_journal, where its chains are written, never to the floor's journal.
``relocate-controls --cid <id> ...``  one-time move of control chains that an earlier run wrote to the floor's journal: each
                           chain (piece 8 failed, no piece 9 event under its id) is appended to the control journal and removed
                           from the floor's; any other id is refused and nothing is moved.

``--omni-home ROOT``  (every verb) the registry home that ``$OMNI_HOME`` in the overlay's paths stands for. The unit passes it, and a
                           hand run must name the home the floor publisher was started with: the floor reads its journal under
                           that home, and the calling shell's own ``OMNI_HOME`` may be another tree.

Exit codes of ``run``: 0 both chains written; 2 the lab-bound check is still running inside its wait bound (nothing written);
3 nothing could be judged (no overlay, stale or unreadable watcher state, no head serves the wanted end). ``tick`` exits 0
when both ends were judged or honestly had no real head, and 3 when the state could not be judged.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.emission_sink import JsonlEmissionSink
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_pr_landing_decision_compute.adapters.watcher_lab_check import (
    WatcherLabCheckCollector,
    WatcherStateError,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8 import (
    load_join_j8_policy,
)
from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_landing_join_j8_live import (
    HandlerLandingJoinJ8Live,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_join_j8_live import (
    OVERLAY_ID,
    EnumJ8Break,
    EnumJ8LiveStatus,
    EnumJ8Want,
    ModelJoinJ8LiveCommand,
    ModelJoinJ8LiveOverlay,
    ModelJoinJ8LiveResult,
)


def _expand(path: str, home: Path | None = None) -> Path:
    """Expand ``~`` and environment variables; ``$OMNI_HOME`` is the unit's ``--omni-home`` when one is given.

    The floor reads its journal under the registry home its publisher was started with, so a hand run must name that
    home with ``--omni-home`` and not lean on whatever ``OMNI_HOME`` the calling shell happens to export.
    """
    text = path
    if home is not None:
        text = text.replace("${OMNI_HOME}", str(home)).replace("$OMNI_HOME", str(home))
    return Path(os.path.expandvars(os.path.expanduser(text)))


def _handler(
    overlay: ModelJoinJ8LiveOverlay,
    inject: EnumJ8Break | None,
    home: Path | None = None,
) -> HandlerLandingJoinJ8Live:
    try:
        state = json.loads(_expand(overlay.watcher_state_path, home).read_text())
    except (OSError, ValueError) as exc:
        raise WatcherStateError(f"watcher state is unreadable: {exc}") from exc
    if inject is None:
        journal = overlay.emission_journal
    elif overlay.control_journal is None:
        raise ValueError(
            "an injected break needs the overlay's control_journal: it is never written to the floor's journal"
        )
    else:
        journal = overlay.control_journal
    return HandlerLandingJoinJ8Live(
        subjects=WatcherLabCheckCollector(state, overlay, datetime.now(UTC), inject),
        sink=JsonlEmissionSink(_expand(journal, home)),
        overlay=overlay,
        policy=load_join_j8_policy(),
        clock=lambda: datetime.now(UTC),
    )


def _emit(result: ModelJoinJ8LiveResult) -> None:
    sys.stdout.write(result.model_dump_json() + "\n")
    sys.stdout.flush()


def _run(arguments: argparse.Namespace) -> int:
    overlay = load_structured_overlay(OVERLAY_ID, ModelJoinJ8LiveOverlay)
    inject = EnumJ8Break(arguments.inject) if arguments.inject else None
    result = _handler(overlay, inject, arguments.home).handle(
        ModelJoinJ8LiveCommand(
            correlation_id=arguments.correlation_id or uuid.uuid4().hex,
            want=EnumJ8Want(arguments.want),
            pr=arguments.pr,
            inject=inject,
        )
    )
    _emit(result)
    if result.status is EnumJ8LiveStatus.COMPLETED:
        return 0
    return 2 if result.status is EnumJ8LiveStatus.PENDING else 3


def write_evidence(
    state_dir: Path, results: dict[str, ModelJoinJ8LiveResult], code: int
) -> None:
    """The unit's evidence, written after every tick and replaced whole."""
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / "last-run.json"
    temporary = state_dir / f".last-run.{os.getpid()}.json"
    temporary.write_text(
        json.dumps(
            {
                "finished_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "exit_code": code,
                "ends": {
                    end: {
                        "status": r.status.value,
                        "pr": r.pr,
                        "head_sha": r.head_sha,
                        "outcome": r.outcome.value if r.outcome else None,
                        "correlation_id": r.correlation_id,
                    }
                    for end, r in results.items()
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    temporary.replace(target)


def relocate_controls(
    overlay: ModelJoinJ8LiveOverlay, cids: Sequence[str], home: Path | None = None
) -> dict[str, int]:
    """Move control chains out of the floor's journal into the control journal; all or nothing.

    A chain moves only when it is the silent-landing shape: a piece 8 failure under the id and no piece 9 event. A chain
    that piece 9 answered, or an id the journal does not hold, is a real chain or no chain, and refuses the whole move.
    """
    if overlay.control_journal is None:
        raise ValueError(
            "relocating control chains needs the overlay's control_journal"
        )
    floor = _expand(overlay.emission_journal, home)
    try:
        lines = floor.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ValueError(f"the floor's journal is unreadable: {exc}") from exc
    wanted = set(cids)
    moved: list[str] = []
    kept: list[str] = []
    seen: dict[str, dict[str, bool]] = {}
    for line in lines:
        try:
            row = json.loads(line)
            cid = row.get("correlation_id")
        except ValueError:
            cid = None
        if cid in wanted:
            moved.append(line)
            flags = seen.setdefault(cid, {"piece8_failure": False, "piece9": False})
            flags["piece9"] |= row.get("piece") == 9
            flags["piece8_failure"] |= (
                row.get("piece") == 8 and row.get("phase") == "failure"
            )
        else:
            kept.append(line)
    for cid in sorted(wanted):
        shape = seen.get(cid)
        if shape is None or shape["piece9"] or not shape["piece8_failure"]:
            raise ValueError(
                f"{cid} is not a control chain in the floor's journal {floor} ({len(lines)} lines read; needs a piece 8 "
                "failure and no piece 9 event); nothing moved"
            )
    control = _expand(overlay.control_journal, home)
    control.parent.mkdir(parents=True, exist_ok=True)
    with control.open("a", encoding="utf-8") as handle:
        handle.writelines(moved)
    temporary = floor.with_name(f".{floor.name}.{os.getpid()}.tmp")
    temporary.write_text("".join(kept), encoding="utf-8")
    temporary.replace(floor)
    return {"chains": len(wanted), "events": len(moved)}


def _relocate(arguments: argparse.Namespace) -> int:
    overlay = load_structured_overlay(OVERLAY_ID, ModelJoinJ8LiveOverlay)
    sys.stdout.write(
        json.dumps(relocate_controls(overlay, arguments.cid, arguments.home)) + "\n"
    )
    return 0


def _tick(state_dir: Path | None, home: Path | None = None) -> int:
    """What the host unit runs: the golden end, then the error end, each its own real head and correlation id."""
    overlay = load_structured_overlay(OVERLAY_ID, ModelJoinJ8LiveOverlay)
    handler = _handler(overlay, None, home)
    results: dict[str, ModelJoinJ8LiveResult] = {}
    for want in (EnumJ8Want.GOLDEN, EnumJ8Want.ERROR):
        result = handler.handle(
            ModelJoinJ8LiveCommand(correlation_id=uuid.uuid4().hex, want=want)
        )
        _emit(result)
        results[want.value] = result
    code = (
        3 if any(r.status is EnumJ8LiveStatus.FAILED for r in results.values()) else 0
    )
    if state_dir is not None:
        write_evidence(state_dir, results, code)
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="node_pr_landing_join_j8")
    parser.add_argument(
        "verb", nargs="?", choices=("run", "tick", "relocate-controls"), default="tick"
    )
    parser.add_argument("--omni-home", dest="home", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--want", choices=[w.value for w in EnumJ8Want], default="any")
    parser.add_argument("--pr")
    parser.add_argument("--correlation-id")
    parser.add_argument("--cid", action="append", default=[])
    parser.add_argument("--inject", choices=[b.value for b in EnumJ8Break])
    arguments = parser.parse_args(argv)
    try:
        if arguments.verb == "relocate-controls":
            return _relocate(arguments)
        return (
            _run(arguments)
            if arguments.verb == "run"
            else _tick(arguments.state_dir, arguments.home)
        )
    except (NodeOverlayError, WatcherStateError, ValueError) as exc:
        sys.stderr.write(f"INDETERMINATE {type(exc).__name__}: {exc}\n")
        if arguments.state_dir is not None:
            write_evidence(arguments.state_dir, {}, 3)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
