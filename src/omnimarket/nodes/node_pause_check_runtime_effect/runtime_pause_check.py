# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_pause_check_runtime_effect``: what pauses these pull requests?

Cutover plan step L6 (OMN-24233). pr-land's ``pause_check.sh`` and the drain map's pause pass call
this command and parse none of the ledger themselves. It

1. reads the ledger file (the env var the overlay names) once and decides every finding with
   ``HandlerPauseCheckFindings`` (pure);
2. reads the bus-fed ``holds`` answer ONCE for the whole batch (``node_work_ledger_reader_effect``)
   and decides each pull request with ``HandlerHoldVerdict``: a node answer, HELD or CLEAR, replaces
   the file's HOLD findings (``source=node``); an answer that did not come (reader down, watermark
   stale with no hold) falls back to the file's HOLD findings while the hold verdict overlay allows
   it (until plan step L15), counted and stamped ``source=file-fallback``; a hold line it cannot
   read, or a retired fallback, is UNDECIDED. UNDECIDED is a stop and never CLEAR.

Input: ``--prs-json FILE`` (a list of ``{"pr": "repo#n", "labels": [...], "lane": "..."}``) or one or
more ``--pr repo#n`` with ``--labels a,b`` and ``--lane``; ``--rules-json FILE`` (the drain map's
report types, landing order, parked repositories and declared repository pauses).

Output ``--format lines`` (one pull request): the awk lines, headed by ``HOLDS source=<s>
fallbacks=<n> reads=1``; exit 0 CLEAR, 3 PAUSED, 2 UNDECIDED. ``--format json`` (any number): one
JSON document ``{reads, fallbacks, undecided, results: [...]}`` and exit 0, or 2 when a pull request
is UNDECIDED. Every decision emits one event: ``answered`` (golden) or ``refused`` (error).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import yaml

from omnimarket.models.hold_verdict import (
    EnumHoldVerdict,
    ModelHoldVerdictOverlay,
    ModelHoldVerdictRequest,
)
from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    ModelReaderRequest,
    ModelReaderResult,
)
from omnimarket.models.pause_check import (
    EnumFindingKind,
    EnumPauseHoldSource,
    EnumPauseVerdict,
    ModelFinding,
    ModelPauseCheckEvent,
    ModelPauseCheckPr,
    ModelPauseCheckPrResult,
    ModelPauseCheckRequest,
    ModelPauseCheckRules,
    ModelPauseDecision,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.adapters import (
    InMemoryHoldVerdictSink,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers import HandlerHoldVerdict
from omnimarket.nodes.node_ledger_hold_verdict_effect.overlay import (
    load_overlay as load_hold_overlay,
)
from omnimarket.nodes.node_pause_check_findings_compute.handlers import (
    HandlerPauseCheckFindings,
)
from omnimarket.nodes.node_pause_check_runtime_effect.adapters import (
    InMemoryPauseCheckSink,
    JournalPauseCheckSink,
)
from omnimarket.nodes.node_pause_check_runtime_effect.overlay import load_overlay
from omnimarket.nodes.node_pause_check_runtime_effect.protocols import (
    ProtocolPauseCheckEventSink,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import (
    load_overlay as load_reader_overlay,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

CONTRACT = Path(__file__).resolve().parent / "contract.yaml"
READER_NAME = "pause-check"
# Hold verdict reasons that mean the node did not answer: only these may fall back to the file.
_NOT_ANSWERED = frozenset(
    {"node-unreadable-and-no-file-lines", "watermark-stale-and-no-hold"}
)


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    return dict(yaml.safe_load(path.read_text(encoding="utf-8"))["topics"])


def _bus_holds(
    lines: Sequence[str], hold_ids: Sequence[str]
) -> tuple[ModelFinding, ...]:
    """One HOLD finding per applying hold id, its text the rest of the holds line the bus answered."""
    found: list[ModelFinding] = []
    for hold_id in hold_ids:
        prefix = f"HOLD {hold_id} "
        detail = next(
            (ln[len(prefix) :] for ln in lines if ln.startswith(prefix)), "bus hold"
        )
        found.append(
            ModelFinding(kind=EnumFindingKind.HOLD, text=f"{hold_id}: {detail}")
        )
    return tuple(found)


def decide_batch(
    request: ModelPauseCheckRequest,
    answer: ModelReaderResult,
    findings_handler: HandlerPauseCheckFindings,
    hold_overlay: ModelHoldVerdictOverlay,
    sink: ProtocolPauseCheckEventSink,
    topics: Mapping[str, str],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[ModelPauseDecision]:
    """One decision per pull request, and one event emitted for each."""
    file_results = findings_handler.handle(request).results
    verdict_handler = HandlerHoldVerdict(hold_overlay, InMemoryHoldVerdictSink(), clock)
    decided: list[ModelPauseDecision] = []
    for pr, file_result in zip(request.prs, file_results, strict=True):
        key = f"{pr.repo}#{pr.number}"
        verdict = verdict_handler.handle(
            ModelHoldVerdictRequest(
                pr=key, lane=pr.lane, node_answer=answer, file_fallback_lines=None
            )
        )
        result: ModelPauseCheckPrResult | None
        source = EnumPauseHoldSource.NONE
        if verdict.verdict is not EnumHoldVerdict.UNDECIDED:
            source = EnumPauseHoldSource.NODE
            result = file_result.with_holds(_bus_holds(answer.lines, verdict.hold_ids))
            reason = verdict.reason
        elif hold_overlay.file_fallback_allowed and verdict.reason in _NOT_ANSWERED:
            source = EnumPauseHoldSource.FILE_FALLBACK
            result = file_result
            reason = verdict.reason
        else:
            result, reason = None, verdict.reason
        decision = ModelPauseDecision(
            pr=key, result=result, source=source, reason=reason
        )
        sink.emit(
            ModelPauseCheckEvent(
                topic=topics[
                    "pause_check_refused" if result is None else "pause_check_answered"
                ],
                pr=key,
                verdict=decision.verdict_word,
                source=source,
                reason=reason,
                stops=0 if result is None else result.stops,
            )
        )
        decided.append(decision)
    return decided


def _parse_prs(
    args: argparse.Namespace, document: Mapping[str, object]
) -> tuple[ModelPauseCheckPr, ...]:
    """The pull requests from ``--request-json``, ``--prs-json`` or the ``--pr`` flags."""
    entries: list[dict[str, object]]
    if document:
        entries = cast("list[dict[str, object]]", document["prs"])
    elif args.prs_json:
        entries = json.loads(Path(args.prs_json).read_text(encoding="utf-8"))
    else:
        labels = [x for x in (args.labels or "").split(",") if x]
        entries = [{"pr": pr, "labels": labels, "lane": args.lane} for pr in args.pr]
    prs: list[ModelPauseCheckPr] = []
    for entry in entries:
        repo, _, number = str(entry["pr"]).partition("#")
        lane = entry.get("lane")
        raw_labels = entry.get("labels") or []
        assert isinstance(raw_labels, list)
        prs.append(
            ModelPauseCheckPr(
                repo=repo.rsplit("/", 1)[-1],
                number=int(number),
                lane=str(lane) if lane else None,
                labels=tuple(str(x) for x in raw_labels),
            )
        )
    return tuple(prs)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(prog="node_pause_check_runtime_effect")
    parser.add_argument("--pr", action="append", default=[], help="repo#number")
    parser.add_argument("--prs-json", help="a JSON list of {pr, labels, lane}")
    parser.add_argument("--labels", help="comma-separated labels for --pr")
    parser.add_argument("--lane")
    parser.add_argument("--rules-json")
    parser.add_argument(
        "--request-json",
        help='a JSON document {"prs": [...], "rules": {...}} from a file, or "-" for stdin',
    )
    parser.add_argument("--now", help="UTC stamp an until= is compared against")
    parser.add_argument("--full-scan", action="store_true")
    parser.add_argument("--format", choices=("lines", "json"), default="lines")
    args = parser.parse_args(argv)
    if not (args.pr or args.prs_json or args.request_json):
        parser.error("name a pull request with --pr, --prs-json or --request-json")
    if not (args.rules_json or args.request_json):
        parser.error("name the pause rules with --rules-json or --request-json")
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    hold_overlay = load_hold_overlay(env)
    ledger = env.get(overlay.ledger_env, "").strip()
    try:
        ledger_text = Path(ledger).read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        sys.stderr.write(
            f"pause_check: ledger not readable: {ledger or '<unset>'}: {exc}\n"
        )
        return 2
    document: dict[str, object] = {}
    if args.request_json:
        raw = (
            sys.stdin.read()
            if args.request_json == "-"
            else Path(args.request_json).read_text(encoding="utf-8")
        )
        document = json.loads(raw)
        rules = document["rules"]
    else:
        rules = json.loads(Path(args.rules_json).read_text(encoding="utf-8"))
    request = ModelPauseCheckRequest(
        prs=_parse_prs(args, document),
        rules=ModelPauseCheckRules.model_validate(rules),
        ledger_text=ledger_text,
        now=args.now or clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        full_scan=args.full_scan,
    )
    reader_overlay = load_reader_overlay(env)
    reader = HandlerWorkLedgerReader(
        reader_overlay,
        point_read
        if point_read is not None
        else build_point_read(reader_overlay.route, env),
        InMemoryEventSink(),
    )
    answer = reader.handle(
        ModelReaderRequest(reader=READER_NAME, query=EnumReaderQuery.HOLDS)
    )
    journal = env.get(overlay.journal_env, "").strip()
    sink: ProtocolPauseCheckEventSink = (
        JournalPauseCheckSink(Path(journal)) if journal else InMemoryPauseCheckSink()
    )
    decided = decide_batch(
        request,
        answer,
        HandlerPauseCheckFindings(overlay),
        hold_overlay,
        sink,
        load_topics(),
        clock,
    )
    fallbacks = sum(d.source is EnumPauseHoldSource.FILE_FALLBACK for d in decided)
    undecided = [d.pr for d in decided if d.result is None]
    for d in decided:
        sys.stderr.write(
            f"PAUSE-CHECK pr={d.pr} verdict={d.verdict_word} "
            f"source={d.source.value} reason={d.reason}\n"
        )
    if args.format == "json":
        sys.stdout.write(
            json.dumps(
                {
                    "reads": 1,
                    "fallbacks": fallbacks,
                    "undecided": undecided,
                    "results": [d.as_json() for d in decided],
                }
            )
            + "\n"
        )
        return 2 if undecided else 0
    first = decided[0]
    if first.result is None:
        sys.stderr.write(
            f"pause_check: could not decide {first.pr} ({first.reason}); a stop, never CLEAR\n"
        )
        return 2
    sys.stdout.write(
        f"HOLDS source={first.source.value} fallbacks={fallbacks} reads=1\n"
    )
    sys.stdout.write("\n".join(first.result.render_lines()) + "\n")
    return 3 if first.result.verdict is EnumPauseVerdict.PAUSED else 0
