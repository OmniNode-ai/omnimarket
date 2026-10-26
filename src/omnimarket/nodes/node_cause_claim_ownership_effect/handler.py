"""Who owns a shared red check: one rule for the merge pulse and the throughput tick (OMN-19998).

2026-10-18T15:44Z: omniclaude's ``Hostile Review Gate`` was red on 5 PRs. Its job needs ``Hostile Reviewer
(adversarial gate)``, and lane cause-omniclaude-hostile-reviewer-1510 held a live CLAIM with
``cause=omniclaude:Hostile Reviewer (adversarial gate)``. The merge pulse read the gate as inheriting the root and
OWNED, but earlier that day a CLAIM naming the gate itself had not cleared it, and the throughput tick read only the
controller's leases, so it asked for a cause lane every run. The rule, in ``cause_family``: a CLAIM on a root owns every
check that inherits it through the workflow's needs graph, and a CLAIM on an inheriting gate is a CLAIM on its root.

Moved here so the scripts that call it shrink (canonical-file-shape ratchet).

Exception class cause-claim-check-name-unmatched (OMN-17427): 2026-10-19T21:43Z the tick read omnibase_infra's
``CI Summary`` UNOWNED while lane cause-omnibase_infra-ci-summary-2125 held a live CLAIM with
``cause=omnibase_infra:CI_Summary``, and its lab fill dispatched a duplicate cause lane; RSD's ``Hostile Review Gate``
was claimed as ``Hostile_Review_Gate``. ``canonical_check`` is the one spelling both sides are compared in (case,
spaces, underscores, hyphens), ``spelling_only_match`` is the class's detector, and a live cause cell that names the
repository and only a near spelling of the red (``near_miss_cells``), which the rule cannot resolve, is emitted to the
exception queue by the tick's handler.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
import os
import re
import sys
import types
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.handlers.rules_merge_sweep import leased_lanes

from .models import (
    ModelCauseClaimOwnershipRequest,
    ModelCauseClaimOwnershipResult,
    ModelProcessFixOwnershipRequest,
    ModelProcessFixOwnershipResult,
)


def cause_family(context: str, inherited: list[Any] | None) -> list[str]:
    """Every check one cause CLAIM on ``context`` owns, given merge_pulse's ``[context, count, roots]`` inherited cells.

    A gate that needs a failed check has no cause of its own: its cause is that root. So the family of a root is the
    root and every check inheriting from it, the family of an inheriting gate is the family of each of its roots
    (siblings included), and any other check is its own family. Sorted, so the readers that share it agree."""
    cells = {str(c): [str(r) for r in roots] for c, _, roots in inherited or ()}
    seeds = set(cells.get(context, ())) or {context}
    return sorted(
        {context, *seeds, *(c for c, roots in cells.items() if seeds & set(roots))}
    )


CHECK_NAME_CLASS = "cause-claim-check-name-unmatched"
_SPELLING = re.compile(r"[\s_-]+")
NEAR_MISS_RATIO = 0.85


def canonical_check(name: str) -> str:
    """A check name in the one spelling every reader compares: case folded, and each run of spaces, underscores and
    hyphens one space. ``CI Summary``, ``CI_Summary`` and ``ci-summary`` are one check (OMN-17427)."""
    return _SPELLING.sub(" ", str(name).casefold()).strip()


def _canonical_repo(repo: str) -> str:
    return str(repo).strip().split("/")[-1].casefold()


def cause_cells(text: str) -> list[tuple[str, str]]:
    """The ``cause=<repo>:<check>`` cells on a row's first line, as (repo, check)."""
    out = []
    for cell in (c.strip() for c in text.split("\n", 1)[0].split("|")):
        if cell.startswith("cause="):
            cause_repo, _, check = cell[len("cause=") :].strip().partition(":")
            out.append((cause_repo.strip(), check.strip()))
    return out


def claim_names_cause(text: str, repo: str, context: str) -> bool:
    """A CLAIM names the cause when a ``cause=<repo>:<check>`` cell matches, or the check name AND the repo are
    on its first line. A CLAIM that names a PR names no cause (plan 2.3: per-PR CLAIMs never own). The check is
    compared in ``canonical_check``'s spelling on both sides, and the repository case folded (OMN-17427)."""
    want = canonical_check(context)
    for cause_repo, check in cause_cells(text):
        if (
            _canonical_repo(cause_repo) == _canonical_repo(repo)
            and canonical_check(check) == want
        ):
            return True
    first = text.split("\n", 1)[0]
    low = first.lower()
    if re.search(rf"(?<![\w.-]){re.escape(repo.lower())}(?![\w.-])", low) is None:
        return False
    # the lane and run names are slugs of anything; only the row's other cells are read in the canonical spelling
    text_cells = canonical_check(
        " | ".join(
            c for c in first.split("|") if not c.strip().startswith(("lane=", "run="))
        )
    )
    return context.lower() in low or bool(
        want and re.search(rf"(?<![a-z0-9]){re.escape(want)}(?![a-z0-9])", text_cells)
    )


def spelling_only_match(text: str, repo: str, context: str) -> list[str]:
    """The detector of class cause-claim-check-name-unmatched: the CLAIM's cause cells for ``repo`` that name
    ``context`` in another spelling only (the exact, case-folded comparison before OMN-17427 missed them)."""
    want = canonical_check(context)
    return [
        check
        for cause_repo, check in cause_cells(text)
        if _canonical_repo(cause_repo) == _canonical_repo(repo)
        and canonical_check(check) == want
        and check.casefold() != context.strip().casefold()
    ]


def near_miss_cells(text: str, repo: str, context: str) -> list[str]:
    """The cause cells for ``repo`` whose check is a near spelling of ``context`` but not the same check in
    ``canonical_check``'s spelling: one contains the other as whole words, or they match at ``NEAR_MISS_RATIO``.
    The rule cannot tell a typo from another check, so these are the cases left to the exception queue."""
    want = canonical_check(context)
    out = []
    for cause_repo, check in cause_cells(text):
        got = canonical_check(check)
        if (
            _canonical_repo(cause_repo) != _canonical_repo(repo)
            or not got
            or not want
            or got == want
        ):
            continue
        short, long = sorted((got, want), key=len)
        contained = (
            re.search(rf"(?<![a-z0-9]){re.escape(short)}(?![a-z0-9])", long) is not None
        )
        if (
            contained
            or difflib.SequenceMatcher(None, got, want).ratio() >= NEAR_MISS_RATIO
        ):
            out.append(check)
    return out


def stale_base_reds(
    watcher: dict[str, Any] | None,
    registry_root: Path,
    now: datetime,
    only_repo: str | None = None,
    *,
    include_unarmed: bool = False,
) -> dict[str, dict[str, str]]:
    """PR -> check -> refresh evidence for a red predating a merged, explicitly named base fix.

    Fix-Check is the cause-fix guard's identity. An explicit fix must be in this
    base and absent from this head; the red must predate it. Without a declared
    fix, reuse the controller's shared-base barrier before any cause worker.
    """
    # node_cause_fix_guard_compute and node_landing_shared_base_effect move in their own slices.
    from omnimarket.nodes.node_cause_fix_guard_compute.handler import (
        fix_check,
    )
    from omnimarket.nodes.node_landing_shared_base_effect.handler import (
        clone_contains_base,
        shared_red_members,
    )

    state = watcher or {}
    records = {
        k: r
        for k, r in (state.get("prs") or {}).items()
        if not only_repo or k.partition("#")[0] == only_repo
    }
    latest_of: dict[str, dict[str, list[Any]]] = {}
    red_conclusions = {"failure", "timed_out", "startup_failure", "action_required"}
    for key, rec in records.items():
        ci = rec.get("ci") or {}
        latest: dict[Any, Any] = {}
        for row in ci.get("runs") or ():
            if len(row) >= 4 and (
                row[0] not in latest or str(row[3]) >= str(latest[row[0]][3])
            ):
                latest[row[0]] = row
        latest_of[key] = latest
        red = [
            c
            for c in ci.get("red") or ()
            if c not in latest
            or (latest[c][1] == "completed" and latest[c][2] in red_conclusions)
        ]
        records[key] = {**rec, "ci": {**ci, "red": red}}
    merges = state.get("merges") or {}
    fixes: dict[tuple[str, str, str], list[tuple[datetime, str, str]]] = {}

    def when(value: Any) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else None
        except ValueError:
            return None

    def action(key: str, rec: dict[str, Any]) -> str:
        conflicting = (
            str((rec.get("review") or {}).get("merge_state") or "").upper() == "DIRTY"
        )
        command = (
            f"rebase lane rebase-{key.replace('#', '-')} --pr {key}"
            if conflicting
            else f"landing controller update-branch for {key}"
        )
        return (
            "await current checks, then "
            if (rec.get("ci") or {}).get("pending")
            else ""
        ) + command

    for key, rec in records.items():
        facts, merged = rec.get("facts") or {}, merges.get(key) or {}
        at = when(facts.get("merged_at") or merged.get("merged_at"))
        sha = str(merged.get("merge_sha") or "")
        repo, base = (
            key.partition("#")[0],
            str(facts.get("base") or merged.get("base") or ""),
        )
        if (
            facts.get("state") != "MERGED"
            or at is None
            or at > now
            or not re.fullmatch(r"[0-9a-f]{40}", sha)
            or not base
        ):
            continue
        for check in re.findall(
            r"^Fix-Check:\s*(.+)$",
            str(facts.get("body") or ""),
            re.MULTILINE | re.IGNORECASE,
        ):
            fixes.setdefault((repo, base, fix_check(check)), []).append((at, key, sha))
    out: dict[str, dict[str, str]] = {}
    ancestry: dict[tuple[str, str, str], bool | None] = {}

    def contains(repo: str, head: str, fix: str) -> bool | None:
        key = (repo, head, fix)
        if key not in ancestry:
            ancestry[key] = clone_contains_base(registry_root / repo, head, fix)
        return ancestry[key]

    for key, rec in records.items():
        facts, ci = rec.get("facts") or {}, rec.get("ci") or {}
        repo, base, head = (
            key.partition("#")[0],
            str(facts.get("base") or ""),
            str(facts.get("head_sha") or ""),
        )
        if (
            facts.get("state") != "OPEN"
            or ci.get("sha") != head
            or not re.fullmatch(r"[0-9a-f]{40}", head)
        ):
            continue
        tip = str(
            ((state.get("branches") or {}).get(f"{repo}@{base}") or {}).get("sha") or ""
        )
        if not re.fullmatch(r"[0-9a-f]{40}", tip):
            continue
        for check in ci.get("red") or ():
            if check in latest_of[key]:
                red_at = when(latest_of[key][check][3])
            else:
                red_at = max(
                    (
                        t
                        for r in ci.get("red_facts") or ()
                        if len(r) >= 4
                        and r[0] == check
                        and (t := when(r[3])) is not None
                    ),
                    default=None,
                )
            if red_at is None:
                continue
            for at, fix_pr, sha in sorted(
                fixes.get((repo, base, fix_check(check)), []), reverse=True
            ):
                if (
                    red_at < at
                    and contains(repo, tip, sha) is True
                    and contains(repo, head, sha) is False
                ):
                    out.setdefault(key, {})[str(check)] = (
                        f"{key}: {check} red at {red_at:%Y-%m-%dT%H:%M:%SZ} predates {fix_pr} merged "
                        f"{at:%Y-%m-%dT%H:%M:%SZ}; base {base} contains fix {sha}, head {head} lacks it: {action(key, rec)}"
                    )
                    break
    # The existing controller barrier also refreshes shared reds with no declared
    # fix. Reuse its group eligibility, not a second policy inferred from titles.
    defaults = {
        str(b.get("repo")): str(b.get("branch"))
        for b in (state.get("branches") or {}).values()
        if isinstance(b, dict) and b.get("default")
    }
    live = {
        key: types.SimpleNamespace(
            state=(r.get("facts") or {}).get("state"),
            armed=(r.get("facts") or {}).get("armed") or include_unarmed,
            draft=(r.get("facts") or {}).get("draft"),
            head=(r.get("facts") or {}).get("head_sha"),
            base=(r.get("facts") or {}).get("base"),
            rollup="FAILURE" if (r.get("ci") or {}).get("red") else "",
        )
        for key, r in records.items()
    }
    for base_key, check, members in shared_red_members(records, live, defaults):
        repo, _, base = base_key.partition(":")
        tip = str(
            ((state.get("branches") or {}).get(f"{repo}@{base}") or {}).get("sha") or ""
        )
        for key in members:
            if check in out.get(key, {}):
                continue
            head = str(live[key].head or "")
            if (
                re.fullmatch(r"[0-9a-f]{40}", tip)
                and contains(repo, head, tip) is False
            ):
                out.setdefault(key, {})[check] = (
                    f"{key}: {check} shared-base barrier; head {head} lacks landing base {base} {tip}: "
                    f"{action(key, records[key])} before new cause diagnosis"
                )
    return out


def live_cause_claims(
    qs: Any, registry_root: Path, now: Any, ledger: Path | None
) -> list[tuple[str, str]]:
    """(lane, CLAIM first line) of every open, live, non-dispatcher CLAIM carrying a ``cause=`` cell: the rows
    ``qs.shared_red_owners`` reads, with its window and its 45-minute liveness. A silent lane is live while it
    holds a lane-work lease (OMN-21428)."""
    dm = qs.load_drain_map_module()
    grammar = dm.load_grammar(registry_root)
    rows, _ = dm.load_rows(registry_root, 1.0, now, grammar, ledger=ledger)
    leased = leased_lanes(
        [{"type": r.rtype, "lane": r.lane, "text": r.text} for r in rows]
    )
    claims = dm.open_claims(rows, grammar, now - timedelta(days=1))
    context = qs.owner_context(rows, dm)
    out = []
    for c in claims:
        lane, stamp = c.lane or "", context["last_row"].get(c.lane or "", "")
        try:
            live = qs.parse_stamp(stamp) >= now - qs.SILENT_AFTER
        except ValueError:
            live = False
        if (
            (live or lane in leased)
            and lane not in context["dispatchers"]
            and cause_cells(c.text)
        ):
            out.append((lane, c.text.split("\n", 1)[0]))
    return out


def emit_near_misses(
    request: Any, qs: Any, unowned: list[tuple[dict[str, Any], Any]]
) -> list[str]:
    """For each red still unowned, a live CLAIM's cause cell that names its repository and a near spelling of it goes
    to the exception queue, once per open case (the queue's own repeat rule); returns one NOTE line per case. The
    MISSING stays: who owns the red is the operator's or the orchestrator's call, not this rule's."""
    from omnimarket.nodes.node_exception_queue_effect import handler as exceptions
    from omnimarket.nodes.node_exception_queue_effect import models as exception_models

    claims = live_cause_claims(
        qs, Path(request.registry_root), request.now, request.ledger
    )
    lines = []
    for v, _shared in unowned:
        repo, check = str(v["repo"]), str(v["check"])
        near = [
            (lane, cell)
            for lane, text in claims
            for cell in near_miss_cells(text, repo, check)
        ]
        if not near:
            continue
        named = ", ".join(f"{lane} cause={repo}:{cell}" for lane, cell in near)
        row = exception_models.ModelExceptionRow(
            cls=CHECK_NAME_CLASS,
            key=f"{repo}:{canonical_check(check).replace(' ', '-')}",
            source="throughput-tick repo-status owner match",
            evidence=f"{check} red on {repo} read UNOWNED; live cause CLAIM(s) name a near spelling: {named}",
            tried=f"canonical check match (case, spaces, underscores, hyphens): {canonical_check(check)!r} matched no cell",
            proposed_fix=f"the lane rewrites its CLAIM with cause={repo}:{check}, or the operator rules the names one check",
            ticket="OMN-17427",
            repo=request.plugin_repo or "",
        )
        how = "not emitted (read-only tick)"
        if request.emit:
            try:
                if not request.plugin_repo:
                    raise ValueError(
                        "plugin_repo is not set; supply the repos overlay's plugin_source"
                    )
                done = exceptions.HandlerExceptionEmit().handle(
                    exception_models.ModelExceptionEmitRequest(
                        state_dir=request.exception_dir or exceptions.state_dir(),
                        row=row,
                        now=request.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        runner=request.runner,
                        parent=request.parent,
                    )
                )
                how = f"exception emit {done.status} id={done.id}"
            except (
                OSError,
                ValueError,
            ) as exc:  # the tick goes on; the NOTE carries why
                how = f"exception emit error: {' '.join(str(exc).split())[:120]}"
        lines.append(
            f"NOTE repo-status:{repo} | {check} read UNOWNED, but {named} names a near spelling; {how}"
        )
    return lines


def claim_owners_of_check(
    qs: Any,
    local: Any,
    registry_root: Path,
    now: Any,
    repo: str,
    check: str,
    ledger: Path | None = None,
) -> Any:
    """``qs.SharedRed`` of ``check`` red on ``repo``'s open PRs, owned by a live cause CLAIM alone.

    The pulse's own rule (``qs.shared_red_owners``) over the same watcher state: the needs graph of the repository's
    workflows on its base branch (git, in the canonical clone) says which reds inherit which root. Armed or not, an
    open PR carrying the red counts. The controller is left out (an empty one): the tick's decision node already judged
    its leases and parks."""
    pulse = qs.load_merge_pulse()
    records = {
        key: {**rec, "facts": {**(rec.get("facts") or {}), "armed": True}}
        for key, rec in local.open_records().items()
        if key.split("#")[0] == repo
    }
    cell = pulse.armed_red_from_state(
        records, needs=lambda r, base: pulse.read_needs_graph(registry_root / r, base)
    ).get(repo, {})
    top, inherited = list(cell.get("top") or []), list(cell.get("inherited") or [])
    if check not in {str(c) for c, _ in top} | {str(c) for c, _, _ in inherited}:
        top.insert(
            0,
            [
                check,
                sum(
                    1
                    for rec in records.values()
                    if check in ((rec.get("ci") or {}).get("red") or ())
                ),
            ],
        )
    raw = getattr(local, "raw", None) or {}
    # This tick already counts every open red, armed or not. Its base-barrier
    # read must see the same heads as its shared-red and CLAIM readers.
    raw = {**raw, "prs": {**(raw.get("prs") or {}), **records}}
    shared = qs.shared_red_owners(
        types.SimpleNamespace(open_records=lambda: records, raw=raw),
        registry_root,
        now,
        repo,
        top,
        None,
        qs.Controller(),
        inherited,
        ledger,
    )
    return next(r for r in shared if r.context == check)


def load_queue_status(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(
        "cause_claim_ownership_queue_status", path
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class HandlerCauseClaimOwnership:
    """Turn the tick's MISSING repo-status line of a shared red a live cause CLAIM owns into a NOTE naming the lane.

    The tick's decision node reads the controller's leases and parks and no ledger row, so a lane working the cause read
    ``shared_red_unowned`` every run and the line asked for a second cause lane. The verdict record stays the node's, so
    its open unit and its one event are unchanged. A ledger or clone that cannot be read leaves the MISSING and adds a
    NOTE saying the CLAIMs were unread."""

    def handle(
        self, request: ModelCauseClaimOwnershipRequest
    ) -> ModelCauseClaimOwnershipResult:
        result = request.result
        unowned = [
            v
            for v in result.get("repo_status") or []
            if isinstance(v, dict)
            and v.get("verdict") == "shared_red_unowned"
            and v.get("check")
        ]
        if not unowned:
            return ModelCauseClaimOwnershipResult(result)
        lines, missing = list(result["lines"]), list(result.get("missing") or [])
        try:
            if not request.registry_root:
                raise RuntimeError("ONEX_REGISTRY_ROOT is not set")
            qs = load_queue_status(request.queue_status)
            records = {
                k: v
                for k, v in ((request.watcher_state or {}).get("prs") or {}).items()
                if isinstance(v, dict) and (v.get("facts") or {}).get("state") == "OPEN"
            }
            local = types.SimpleNamespace(
                open_records=lambda: records, raw=request.watcher_state
            )
            owned = [
                (
                    v,
                    claim_owners_of_check(
                        qs,
                        local,
                        Path(request.registry_root),
                        request.now,
                        str(v["repo"]),
                        str(v["check"]),
                        request.ledger,
                    ),
                )
                for v in unowned
            ]
        except (
            Exception,
            SystemExit,
        ) as exc:  # drain_map exits when the ledger is missing
            why = " ".join(str(exc).split())[:200] or type(exc).__name__
            lines += [
                f"NOTE repo-status:{v['repo']} | cause CLAIMs unread: {why}"
                for v in unowned
            ]
            return ModelCauseClaimOwnershipResult({**result, "lines": lines})
        changed = False
        try:
            near = emit_near_misses(
                request,
                qs,
                [
                    (v, shared)
                    for v, shared in owned
                    if not shared.owners and shared.prs
                ],
            )
        except (
            Exception,
            SystemExit,
        ) as exc:  # drain_map exits when the ledger is missing
            near = [
                f"NOTE repo-status:{v['repo']} | near-spelling cause CLAIMs unread: "
                f"{' '.join(str(exc).split())[:120] or type(exc).__name__}"
                for v, shared in owned
                if not shared.owners
            ]
        if near:
            lines += near
            changed = True
        for v, shared in owned:
            loop = f"repo-status:{v['repo']}"
            index = next(
                (i for i, x in enumerate(lines) if x.startswith(f"MISSING {loop} | ")),
                None,
            )
            if index is not None and shared.refresh and not shared.prs:
                lines[index] = f"NOTE {loop} | stale-base red; " + "; ".join(
                    shared.refresh.values()
                )
                missing = [m for m in missing if m != loop]
                changed = True
                continue
            if index is not None and shared.refresh:
                lines[index] = (
                    f"MISSING {loop} | shared_red_unowned: {v['check']} on {' '.join(shared.prs)} | FIX: "
                    f"{qs.cause_lane_command(shared)}"
                )
                lines.append(
                    f"NOTE shared-base-refresh:{v['repo']} | "
                    + "; ".join(shared.refresh.values())
                )
                changed = True
            if not shared.owners or index is None:
                continue
            inherits = (
                f" ({v['check']} inherits {', '.join(shared.inherits)})"
                if shared.inherits
                else ""
            )
            why = lines[index].removeprefix("MISSING ").split(" | FIX: ", 1)[0]
            lines[index] = (
                f"NOTE {why} | owned by cause CLAIM {', '.join(shared.owners)}{inherits}"
            )
            missing = [m for m in missing if m != loop]
            changed = True
        if not changed:
            return ModelCauseClaimOwnershipResult(result)
        rep = request.report_type(
            lines=lines,
            missing=missing,
            unknown=list(result.get("unknown") or []),
            checked=list(result.get("checked") or []),
        )
        return ModelCauseClaimOwnershipResult(
            {
                **result,
                "lines": lines,
                "status_line": rep.status("THROUGHPUT", "STALL"),
                "exit_code": 1 if rep.missing or rep.unknown else 0,
                "missing": missing,
            }
        )


DECISIONS_TAIL_BYTES = 8 << 20
# The controller is acting on an item only while its newest decision for it is a live lease or a dispatch this recent.
ACTING_WINDOW = timedelta(minutes=30)
# A worker result of one of these kinds (kind, reason or blocker_kind) is an attempt that never ran.
NEVER_RAN = ("lab_capacity_unavailable", "host_tools_refused")


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _short(subject: str) -> str:
    """``Owner/repo#n`` -> ``repo#n``; a cause key is its own name."""
    return (
        subject.rsplit("/", 1)[-1]
        if "#" in subject and not subject.startswith("cause:")
        else subject
    )


def _tail_lines(path: Path, size: int) -> list[str]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        end = stream.tell()
        stream.seek(max(0, end - size))
        data = stream.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if end > size else lines


def read_decisions(landing: Path) -> list[dict[str, Any]]:
    """decisions.jsonl's tail rows in file (time) order; a missing file is a controller that decided nothing."""
    try:
        lines = _tail_lines(landing / "decisions.jsonl", DECISIONS_TAIL_BYTES)
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def lease_released(lease: Mapping[str, Any], state_dir: Path) -> bool:
    """A revoked lease whose release was confirmed (``released_at`` on the entry or on its lease.json). A revoked
    lease still in state.json with no confirmed release may still have a live process: it still owns."""
    if not lease.get("revoked"):
        return False
    if lease.get("released_at"):
        return True
    try:
        data = json.loads(
            (
                state_dir / "workers" / str(lease.get("lease_id")) / "lease.json"
            ).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("released_at"))


_READ: dict[
    str, tuple[tuple[Any, ...], list[dict[str, Any]], dict[str, dict[str, Any]]]
] = {}


def acting(
    landing: Path,
    subject: str,
    now: datetime | None = None,
    lease_id: int | None = None,
) -> tuple[bool, str]:
    """``judge`` over the controller's files, read once per change of decisions.jsonl and state.json; files that cannot
    be read are no acting owner, and the evidence says they were unread."""
    try:
        sign = tuple(
            (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
            for p in (landing / "decisions.jsonl", landing / "state.json")
        )
        if (_READ.get(str(landing)) or ((),))[0] != sign:
            _READ[str(landing)] = (sign, read_decisions(landing), read_leases(landing))
        _, rows, leases = _READ[str(landing)]
    except (OSError, ValueError) as exc:
        return False, f"the controller's files unread ({type(exc).__name__})"
    return judge(rows, leases, landing, subject, now or datetime.now(UTC), lease_id)


def read_leases(landing: Path) -> dict[str, dict[str, Any]]:
    """state.json's leases by lease id; raises when it cannot be read, so an owner nobody read is never an answer."""
    state = json.loads((landing / "state.json").read_text(encoding="utf-8"))
    if not isinstance(state, dict) or not isinstance(state.get("leases"), list):
        raise ValueError("state.json holds no leases list")
    return {
        str(le["lease_id"]): le
        for le in state["leases"]
        if isinstance(le, dict) and le.get("lease_id") is not None
    }


def worker_result(landing: Path, lease_id: object) -> dict[str, Any] | None:
    try:
        data = json.loads(
            (landing / "workers" / str(lease_id) / "result.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def judge(
    rows: list[dict[str, Any]],
    leases: Mapping[str, Mapping[str, Any]],
    landing: Path,
    subject: str,
    now: datetime,
    lease_id: int | None,
    window: timedelta = ACTING_WINDOW,
) -> tuple[bool, str]:
    """(acting, evidence) from the controller's newest decision for ``subject`` (or, with none, for ``lease_id``)."""
    key = _short(subject)
    mine = [
        r for r in rows if _short(str(r.get("subject") or r.get("cause") or "")) == key
    ]
    if not mine and lease_id is not None:
        mine = [r for r in rows if str(r.get("lease_id")) == str(lease_id)]
    if not mine:
        return False, f"no decisions.jsonl row for {key}"
    row = mine[-1]
    at = _when(row.get("ts"))
    age = None if at is None else int((now - at).total_seconds() // 60)
    where = f"tick {row.get('tick', '?')} {row.get('ts', '?')} {row.get('kind', '?')} {row.get('outcome', '?')}"
    if row.get("kind") == "dispatch_worker" and row.get("outcome") == "spawned":
        lid = row.get("lease_id")
        res = worker_result(landing, lid)
        never = next(
            (
                str(v)
                for v in (
                    (res or {}).get(k) for k in ("kind", "reason", "blocker_kind")
                )
                if v in NEVER_RAN
            ),
            None,
        )
        if never:
            return False, (
                f"{where}: workers/{lid}/result.json kind={(res or {}).get('kind')} {never}: the attempt never ran"
            )
        lease = leases.get(str(lid))
        if res is None and lease is not None and not lease.get("revoked"):
            return True, f"{where}: live lease landing-L{lid}"
        if age is not None and timedelta(minutes=age) <= window:
            return True, f"{where} ({age}m ago)"
        return False, (
            f"{where} {'?' if age is None else age}m ago, "
            f"{'result kind=' + str(res.get('kind')) if res else 'no live lease'}"
        )
    detail = " ".join(str(row.get("detail") or "").split())[:200]
    return False, where + (f": {detail}" if detail else "")


PROCESS_FIX_LOOP = "process-fix:"
PROCESS_FIX_DRIVER = "driver lane pf-drive-"


def controller_lease_owners(qs: Any, landing: Path, now: Any) -> dict[str, list[str]]:
    """``repo#n`` -> the controller's unreleased leases covering it, from state.json: a lease on the PR itself, or on a cause
    (``pr=cause:<owner>/<repo>:<signature>``) that lists it in its ``causes`` record's members. A lease the controller
    revoked and confirmed released (``qs.lease_released``) owns nothing; queue-status's ``read_controller`` leases (a
    ``cause`` key with ``members``) count too. Raises when state.json cannot be read, so an owner nobody read is never an empty answer."""
    try:
        state = json.loads((landing / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"controller state.json unreadable ({type(exc).__name__})"
        ) from exc
    if not isinstance(state, dict) or not isinstance(state.get("leases"), list):
        raise ValueError("state.json holds no leases list")
    # OMN-17427: a lease owns only while the controller acts on it (its newest decisions.jsonl row is a live lease or a
    # dispatch in the last 30 minutes); claim-refused, held or an attempt that never ran is not acting. The acting
    # helper lives here.

    labels: dict[str, str] = {}
    owners: dict[str, set[str]] = {}
    for lease in state["leases"]:
        if (
            not isinstance(lease, dict)
            or not lease.get("pr")
            or qs.lease_released(lease, landing)
        ):
            continue
        lid = lease.get("lease_id")
        if not acting(
            landing,
            str(lease["pr"]),
            now,
            int(str(lid)) if str(lid).isdigit() else None,
        )[0]:
            continue
        label = (
            f"controller lease landing-L{lease['lease_id']}"
            if lease.get("lease_id") is not None
            else "controller lease"
        )
        labels[str(lease["pr"])] = label
        if not str(lease["pr"]).startswith("cause:"):
            owners.setdefault(str(lease["pr"]).split("/", 1)[-1], set()).add(label)
    for cause in state.get("causes") or ():
        for member in (
            (cause.get("members") or ())
            if isinstance(cause, dict) and str(cause.get("key")) in labels
            else ()
        ):
            if isinstance(member, dict) and member.get("pr"):
                owners.setdefault(str(member["pr"]).split("/", 1)[-1], set()).add(
                    labels[str(cause["key"])]
                )
    for ref in qs.read_controller(landing, now).leases:
        for member in ref.members:
            owners.setdefault(member, set()).add(f"controller lease {ref.owner}")
    return {pr: sorted(found) for pr, found in owners.items()}


def claim_lane_owners(
    qs: Any, ledger: Path | None, now: Any, refs: list[str]
) -> dict[str, list[str]]:
    """``repo#n`` -> the lanes whose live CLAIM names it: drain_map.py's own row reader and owner match, and its liveness
    rule (a lane with no row for 45 minutes owns nothing; a chain CLAIM 180). A silent lane is live while it holds
    a lane-work lease (OMN-21428). Raises when the ledger cannot be read."""
    dm = qs.load_drain_map_module()
    if not os.environ.get("ONEX_REGISTRY_ROOT", "").strip():
        raise RuntimeError("ONEX_REGISTRY_ROOT is not set")
    home = Path(os.environ["ONEX_REGISTRY_ROOT"])
    grammar = dm.load_grammar(home)
    rows, _ = dm.load_rows(home, 7.0, now, grammar, ledger)
    leased = leased_lanes(
        [{"type": r.rtype, "lane": r.lane, "text": r.text} for r in rows]
    )
    claims = dm.open_claims(rows, grammar, now - timedelta(days=7))
    context = qs.owner_context(rows, dm)

    def live(row_stamp: str, is_chain: bool = False) -> bool:
        try:
            return bool(
                qs.parse_stamp(row_stamp)
                >= now - (dm.CHAIN_DEAD_AFTER if is_chain else dm.DEAD_AFTER)
            )
        except ValueError:
            return False

    found: dict[str, list[str]] = {}
    for ref in refs:
        repo, _, number = ref.partition("#")
        owners = dm.ledger_owners(
            dm.PR(repo, int(number)), rows, claims, live, **context
        )
        found[ref] = sorted(
            {
                f"live CLAIM {o.lane}"
                for o in owners
                if o.kind == "CLAIM" and (o.live or o.lane in leased) and not o.ended
            }
        )
    return found


class HandlerProcessFixOwnership:
    """Turn a process-fix PR's driver-lane MISSING into a NOTE naming its owner when something already owns the PR.

    2026-10-18 19:25-19:45Z: of 12 pf-drive lanes dispatched from the tick's ``MISSING process-fix:<pr>`` lines, three
    ended skipped-owned, because a landing-controller lease or a live CLAIM held the PR. The owner is read the way the merge
    pulse reads it: unreleased controller leases (state.json), cause leases the PR is a member of, and a CLAIM live within
    the 45-minute rule. Owners that cannot be read leave the FIX and say so. Reads files; writes nothing."""

    def handle(
        self, request: ModelProcessFixOwnershipRequest
    ) -> ModelProcessFixOwnershipResult:
        wanted = {
            f[1].removeprefix(PROCESS_FIX_LOOP): i
            for i, f in enumerate(request.found)
            if f[0] == "missing"
            and f[1].startswith(PROCESS_FIX_LOOP)
            and f[3].startswith(PROCESS_FIX_DRIVER)
        }
        if not wanted:
            return ModelProcessFixOwnershipResult(request.found)
        found = list(request.found)
        if request.watcher_state and request.registry_root:
            refresh = stale_base_reds(
                request.watcher_state,
                request.registry_root,
                request.now,
                include_unarmed=True,
            )
            for ref, i in list(wanted.items()):
                ci = ((request.watcher_state.get("prs") or {}).get(ref) or {}).get(
                    "ci"
                ) or {}
                if ci.get("red") and set(ci["red"]) <= refresh.get(ref, {}).keys():
                    _, loop, why, _ = found[i]
                    found[i] = (
                        "note",
                        loop,
                        why + "; stale-base red",
                        "; ".join(refresh[ref].values()),
                    )
                    wanted.pop(ref)
        if not wanted:
            return ModelProcessFixOwnershipResult(found)
        try:
            qs = load_queue_status(request.queue_status)
            leases = controller_lease_owners(qs, request.landing, request.now)
            claims = claim_lane_owners(qs, request.ledger, request.now, sorted(wanted))
        except (
            Exception,
            SystemExit,
        ) as exc:  # drain_map exits when the ledger is missing
            why = " ".join(str(exc).split())[:120] or type(exc).__name__
            for i in wanted.values():
                found[i] = (
                    found[i][0],
                    found[i][1],
                    f"{found[i][2]} (owners unread: {why})",
                    found[i][3],
                )
            return ModelProcessFixOwnershipResult(found)
        for ref, i in wanted.items():
            owners = [*leases.get(ref, []), *claims.get(ref, [])]
            if owners:
                _level, loop, why, _ = found[i]
                found[i] = (
                    "note",
                    loop,
                    f"{loop} | {why} | owned by {', '.join(owners)}; no driver lane",
                    "",
                )
        return ModelProcessFixOwnershipResult(found)
