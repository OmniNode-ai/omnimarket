# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources the contract names into typed records and source reads (the effect side)."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.models.landing_ledger_row import ModelLandingLedgerRow
from omnimarket.models.process_health import (
    EnumReadStatus,
    EnumSourceName,
    ModelControllerDecision,
    ModelControllerLease,
    ModelDelegationRow,
    ModelLandingMetricsWindow,
    ModelLinearSnapshot,
    ModelMergeRecord,
    ModelRepoStatusEvent,
    ModelSourceRead,
    ModelTimeSpan,
)

_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z"
_ROW = re.compile(rf"^(?P<ts>{_STAMP}) \| (?P<type>[A-Z][A-Z-]*) \|(?P<rest>.*)$")
_LANE = re.compile(r"(?:^|\| )lane=([^ |]+)")
_PR_SUBJECT = re.compile(r"#\d+$")
_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_GIT_FIELD = "\x1f"
_GIT_RECORD = "\x1e"
_KEEP_ALWAYS = frozenset({"HOLD", "RELEASE"})


class UnsetEnvError(RuntimeError):
    """A path in the contract needs an environment variable that is not set."""


def expand(template: str, environ: Mapping[str, str]) -> str:
    """Expand ``${VAR}`` and a leading ``~``; an unset variable is an error, never an empty string."""

    def sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in environ:
            raise UnsetEnvError(f"environment variable {name} is not set")
        return environ[name]

    text = _ENV.sub(sub, template)
    return str(Path(text).expanduser()) if text.startswith("~") else text


def display(path: Path | str, environ: Mapping[str, str]) -> str:
    """A locator with the home directory as ~, so no output names a home path."""
    home = environ.get("HOME", "")
    text = str(path)
    return "~" + text[len(home) :] if home and text.startswith(home) else text


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


def _unread(name: EnumSourceName, locator: str, reason: str) -> ModelSourceRead:
    return ModelSourceRead(
        name=name, status=EnumReadStatus.UNREAD, locator=locator, reason=reason
    )


def gaps_between(
    moments: Iterable[datetime], max_gap: timedelta
) -> tuple[ModelTimeSpan, ...]:
    """Spans longer than max_gap between consecutive moments."""
    ordered = sorted(set(moments))
    return tuple(
        ModelTimeSpan(start=before, end=after)
        for before, after in pairwise(ordered)
        if after - before > max_gap
    )


@dataclass
class Sources:
    """Everything the runtime read."""

    reads: list[ModelSourceRead] = field(default_factory=list)
    ledger_rows: list[ModelLandingLedgerRow] = field(default_factory=list)
    repo_status_events: list[ModelRepoStatusEvent] = field(default_factory=list)
    merges: list[ModelMergeRecord] = field(default_factory=list)
    decisions: list[ModelControllerDecision] = field(default_factory=list)
    leases: list[ModelControllerLease] = field(default_factory=list)
    delegation_rows: list[ModelDelegationRow] = field(default_factory=list)
    landing_metrics: list[ModelLandingMetricsWindow] = field(default_factory=list)
    linear_issues: list[Any] = field(default_factory=list)


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def read_ledger(
    io: Mapping[str, Any], environ: Mapping[str, str], keep_from: datetime, out: Sources
) -> None:
    """The live ledger and its archive splits; rows older than keep_from are kept only if HOLD or RELEASE."""
    try:
        live = Path(expand(io["live"], environ))
    except UnsetEnvError as exc:
        out.reads.append(_unread(EnumSourceName.LEDGER, io["live"], str(exc)))
        return
    locator = display(live, environ)
    if not live.is_file():
        out.reads.append(
            _unread(EnumSourceName.LEDGER, locator, "the live ledger file is missing")
        )
        return
    files = [
        *sorted((live.parent / io["archive_dir_name"]).glob(io["archive_glob"])),
        live,
    ]
    seen: set[str] = set()
    first: datetime | None = None
    last: datetime | None = None
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _ROW.match(line.rstrip("\n"))
                if match is None or line in seen:
                    continue
                seen.add(line)
                moment = _parse_ts(match.group("ts"))
                first = moment if first is None else min(first, moment)
                last = moment if last is None else max(last, moment)
                if moment < keep_from and match.group("type") not in _KEEP_ALWAYS:
                    continue
                rest = match.group("rest").strip()
                lane = _LANE.search(rest)
                out.ledger_rows.append(
                    ModelLandingLedgerRow(
                        ts=match.group("ts"),
                        rtype=match.group("type"),
                        lane=lane.group(1) if lane else None,
                        text=rest,
                    )
                )
    if first is None or last is None:
        out.reads.append(
            _unread(EnumSourceName.LEDGER, locator, "no ledger row parsed")
        )
        return
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.LEDGER,
            status=EnumReadStatus.READ,
            locator=f"{locator} and {display(live.parent / io['archive_dir_name'], environ)}/{io['archive_glob']}",
            reason=f"{len(files)} files, {len(seen)} distinct rows parsed",
            covered_from=max(first, keep_from) if first < keep_from else first,
            covered_to=max(last, datetime.fromtimestamp(live.stat().st_mtime, UTC)),
        )
    )


def read_landing(
    io: Mapping[str, Any],
    environ: Mapping[str, str],
    fix_dispatch_kind: str,
    fix_dispatch_outcome: str,
    keep_from: datetime,
    out: Sources,
) -> None:
    """Decisions, cost and delegation; cost and delegation inherit the decisions' liveness gaps."""
    names = (
        EnumSourceName.LANDING_DECISIONS,
        EnumSourceName.LANDING_COST,
        EnumSourceName.LANDING_DELEGATION,
    )
    base = Path(os.path.expanduser(environ.get(io["dir_env_var"], io["dir_default"])))
    base_text = display(base, environ)
    decisions_path = base / io["decisions_file"]
    if not decisions_path.is_file():
        for name in names:
            out.reads.append(
                _unread(name, base_text, f"{decisions_path.name} is missing")
            )
        return
    ticks: list[datetime] = []
    first_for_pr: dict[str, ModelControllerDecision] = {}
    for row in _jsonl(decisions_path):
        raw_ts = row.get("ts")
        if not isinstance(raw_ts, str):
            continue
        moment = _parse_ts(raw_ts)
        ticks.append(moment)
        subject = row.get("subject")
        if not isinstance(subject, str) or _PR_SUBJECT.search(subject) is None:
            continue
        decision = ModelControllerDecision(
            ts=moment,
            pr=subject.rsplit("/", 1)[-1],
            head=str(row.get("head_sha", "")),
            kind=str(row.get("kind", "")),
            outcome=str(row.get("outcome", "")),
        )
        first_for_pr.setdefault(decision.pr, decision)
        if (
            decision.kind == fix_dispatch_kind
            and decision.outcome == fix_dispatch_outcome
            and moment >= keep_from
        ):
            out.decisions.append(decision)
    out.decisions.extend(
        d
        for d in first_for_pr.values()
        if not (d.kind == fix_dispatch_kind and d.outcome == fix_dispatch_outcome)
        or d.ts < keep_from
    )
    if not ticks:
        for name in names:
            out.reads.append(
                _unread(name, base_text, "the decisions file holds no timestamped row")
            )
        return
    gaps = gaps_between(ticks, timedelta(hours=float(io["max_gap_hours"])))
    covered = {"covered_from": min(ticks), "covered_to": max(ticks), "gaps": gaps}
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.LANDING_DECISIONS,
            status=EnumReadStatus.READ,
            locator=f"{base_text}/{io['decisions_file']}",
            reason=f"{len(ticks)} rows; spans with no controller row longer than {io['max_gap_hours']} h are gaps",
            **covered,
        )
    )
    inherited = "inherits the decisions file's liveness gaps: no controller tick means no lease was recorded"
    for name, file_name in (
        (EnumSourceName.LANDING_COST, io["cost_file"]),
        (EnumSourceName.LANDING_DELEGATION, io["delegation_file"]),
    ):
        path = base / file_name
        if not path.is_file():
            out.reads.append(_unread(name, base_text, f"{file_name} is missing"))
            continue
        if name is EnumSourceName.LANDING_COST:
            for row in _jsonl(path):
                try:
                    if row.get("kind") not in ("pr", "cause"):
                        continue
                    out.leases.append(
                        ModelControllerLease(
                            kind=str(row["kind"]),
                            subject=str(row.get("subject", "")),
                            repo=str(row.get("repo", "")),
                            spawned_at=_parse_ts(str(row["spawned_at"])),
                            released_at=_parse_ts(str(row["released_at"])),
                            worker_minutes=float(row.get("worker_minutes", 0.0)),
                        )
                    )
                except (KeyError, ValueError, ValidationError):
                    continue
        else:
            for row in _jsonl(path):
                try:
                    out.delegation_rows.append(
                        ModelDelegationRow(
                            ts=_parse_ts(str(row["ts"])),
                            lease_id=int(row["lease_id"]),
                            delegated=int(row.get("delegated", 0)),
                        )
                    )
                except (KeyError, ValueError, ValidationError):
                    continue
        out.reads.append(
            ModelSourceRead(
                name=name,
                status=EnumReadStatus.READ,
                locator=f"{base_text}/{file_name}",
                reason=inherited,
                **covered,
            )
        )


def read_repo_status(
    io: Mapping[str, Any], environ: Mapping[str, str], out: Sources
) -> None:
    """The throughput tick's verdict-change log; a span with no event for any repo longer than max_gap is a gap."""
    path = Path(os.path.expanduser(environ.get(io["env_var"], io["default"])))
    locator = display(path, environ)
    if not path.is_file():
        out.reads.append(
            _unread(
                EnumSourceName.REPO_STATUS_EVENTS,
                locator,
                "the repo-status log is missing",
            )
        )
        return
    moments: list[datetime] = []
    for row in _jsonl(path):
        try:
            event = ModelRepoStatusEvent(
                at=_parse_ts(str(row["at"])),
                repo=str(row["repo"]),
                verdict=str(row["verdict"]),
                check=str(row["check"]) if row.get("check") else None,
            )
        except (KeyError, ValueError, ValidationError):
            continue
        out.repo_status_events.append(event)
        moments.append(event.at)
    if not moments:
        out.reads.append(
            _unread(
                EnumSourceName.REPO_STATUS_EVENTS,
                locator,
                "the repo-status log holds no event",
            )
        )
        return
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.REPO_STATUS_EVENTS,
            status=EnumReadStatus.READ,
            locator=locator,
            reason=f"{len(moments)} verdict events; spans with no event for any repo longer than {io['max_gap_hours']} h are gaps",
            covered_from=min(moments),
            covered_to=max(moments),
            gaps=gaps_between(moments, timedelta(hours=float(io["max_gap_hours"]))),
        )
    )


def read_landing_metrics(
    io: Mapping[str, Any], environ: Mapping[str, str], out: Sources
) -> None:
    """The landing-metrics history; consecutive windows with a hole between them are gaps."""
    path = Path(
        expand(io["default"], {**environ})
        if io["env_var"] not in environ
        else environ[io["env_var"]]
    )
    locator = display(path, environ)
    if not path.is_file():
        out.reads.append(
            _unread(
                EnumSourceName.LANDING_METRICS_HISTORY,
                locator,
                "the history file is missing",
            )
        )
        return
    windows: list[tuple[datetime, datetime]] = []
    for row in _jsonl(path):
        if row.get("baseline") is True:
            continue
        try:
            start, end = (
                _parse_ts(str(row["window_start"])),
                _parse_ts(str(row["window_end"])),
            )
            median = row.get("median_green_to_merged_min")
            out.landing_metrics.append(
                ModelLandingMetricsWindow(
                    window_end=end,
                    green_to_merged_n=int(row.get("green_to_merged_n", 0)),
                    median_green_to_merged_min=None
                    if median is None
                    else float(median),
                )
            )
            windows.append((start, end))
        except (KeyError, ValueError, ValidationError):
            continue
    if not windows:
        out.reads.append(
            _unread(
                EnumSourceName.LANDING_METRICS_HISTORY,
                locator,
                "the history holds no window",
            )
        )
        return
    ordered = sorted(windows)
    gaps = tuple(
        ModelTimeSpan(start=before[1], end=after[0])
        for before, after in pairwise(ordered)
        if after[0] - before[1] > timedelta(minutes=10)
    )
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.LANDING_METRICS_HISTORY,
            status=EnumReadStatus.READ,
            locator=locator,
            reason=f"{len(windows)} measured windows; holes between windows are gaps",
            covered_from=ordered[0][0],
            covered_to=max(end for _, end in ordered),
            gaps=gaps,
        )
    )


def read_linear(
    io: Mapping[str, Any], environ: Mapping[str, str], out: Sources
) -> None:
    """The Linear snapshot file; absent means the Linear metrics are unread."""
    try:
        path = Path(
            environ[io["env_var"]]
            if io["env_var"] in environ
            else expand(io["default"], environ)
        )
    except UnsetEnvError as exc:
        out.reads.append(
            _unread(EnumSourceName.LINEAR_SNAPSHOT, io["default"], str(exc))
        )
        return
    locator = display(path, environ)
    if not path.is_file():
        out.reads.append(
            _unread(
                EnumSourceName.LINEAR_SNAPSHOT,
                locator,
                "no Linear snapshot file has been written",
            )
        )
        return
    try:
        snapshot = ModelLinearSnapshot.model_validate_json(
            path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        out.reads.append(
            _unread(
                EnumSourceName.LINEAR_SNAPSHOT,
                locator,
                f"the snapshot is unreadable ({type(exc).__name__})",
            )
        )
        return
    out.linear_issues.extend(snapshot.issues)
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.LINEAR_SNAPSHOT,
            status=EnumReadStatus.READ,
            locator=locator,
            reason=f"{len(snapshot.issues)} issues",
            covered_from=snapshot.covered_from,
            covered_to=snapshot.covered_to,
        )
    )


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def _default_ref(repo: Path, candidates: list[str]) -> str | None:
    head = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    ordered = ([head.strip()] if head else []) + candidates
    for ref in ordered:
        if _git(repo, "rev-parse", "--verify", "--quiet", ref) is not None:
            return ref
    return None


def _is_docs(files: list[str], repo_name: str, io: Mapping[str, Any]) -> bool:
    if any(marker in repo_name for marker in io["docs_repo_markers"]):
        return True
    return bool(files) and all(
        name.endswith(tuple(io["docs_suffixes"]))
        or name.startswith(tuple(io["docs_dir_prefixes"]))
        for name in files
    )


def read_git(
    io: Mapping[str, Any], environ: Mapping[str, str], since: datetime, out: Sources
) -> None:
    """First-parent squash commits on each clone's default branch since a cut-off."""
    roots = (
        [Path(environ[io["clone_root_env_var"]])]
        if io["clone_root_env_var"] in environ
        else []
    )
    if roots and not roots[0].is_dir():
        out.reads.append(
            _unread(
                EnumSourceName.GIT_MERGES,
                display(roots[0], environ),
                f"{io['clone_root_env_var']} names a directory that does not exist",
            )
        )
        return
    if not roots:
        out.reads.append(
            _unread(
                EnumSourceName.GIT_MERGES,
                io["clone_root_env_var"],
                f"environment variable {io['clone_root_env_var']} is not set",
            )
        )
        return
    registry = [Path(p) for p in environ.get("ONEX_REGISTRY_ROOTS", "").split(":") if p]
    repos = sorted(
        {
            *(c for c in roots[0].iterdir() if (c / ".git").exists()),
            *(r for r in registry if (r / ".git").exists()),
        }
    )
    fetched: list[datetime] = []
    counted = 0
    for repo in repos:
        ref = _default_ref(repo, list(io["default_branch_refs"]))
        if ref is None:
            continue
        common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            fetch_head = Path(common.strip()) / "FETCH_HEAD"
            if fetch_head.exists():
                fetched.append(datetime.fromtimestamp(fetch_head.stat().st_mtime, UTC))
        log = _git(
            repo,
            "log",
            "--first-parent",
            f"--since={since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            f"--format={_GIT_RECORD}%H{_GIT_FIELD}%ct{_GIT_FIELD}%an{_GIT_FIELD}%s",
            "--name-only",
            ref,
        )
        if log is None:
            continue
        for record in log.split(_GIT_RECORD)[1:]:
            head, _, names = record.partition("\n")
            parts = head.split(_GIT_FIELD)
            if len(parts) != 4:
                continue
            match = _PR_SUFFIX.search(parts[3])
            if match is None:
                continue
            files = [n for n in names.splitlines() if n]
            out.merges.append(
                ModelMergeRecord(
                    repo=repo.name,
                    number=int(match.group(1)),
                    committed_at=datetime.fromtimestamp(int(parts[1]), UTC),
                    author=parts[2],
                    subject=parts[3],
                    docs_only=_is_docs(files, repo.name, io),
                    ledger_only=bool(files)
                    and all(
                        any(m in n for m in io["ledger_path_markers"]) for n in files
                    ),
                )
            )
            counted += 1
    locator = f"{display(roots[0], environ)}/*"
    if not fetched:
        out.reads.append(
            _unread(
                EnumSourceName.GIT_MERGES,
                locator,
                "no clone had a default-branch ref and a FETCH_HEAD",
            )
        )
        return
    out.reads.append(
        ModelSourceRead(
            name=EnumSourceName.GIT_MERGES,
            status=EnumReadStatus.READ,
            locator=locator,
            reason=f"{counted} squash commits from {len(repos)} clones; as fresh as the oldest FETCH_HEAD",
            covered_from=since,
            covered_to=min(fetched),
        )
    )
