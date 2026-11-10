# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read external sources into the factory floor's typed input facts."""

from __future__ import annotations

import json
import os
import re
import runpy
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.models.model_factory_floor_input import (
    CiState,
    ModelFactoryFloorConfig,
    ModelFloorCloudAccount,
    ModelFloorContext,
    ModelFloorDriftHost,
    ModelFloorGoalRow,
    ModelFloorHostDrift,
    ModelFloorHostRow,
    ModelFloorLaneProcess,
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorMerge,
    ModelFloorPlacedHost,
    ModelFloorPlanInput,
    ModelFloorPoolDisk,
    ModelFloorPoolReading,
    ModelFloorPr,
    ModelFloorReceipt,
    ModelFloorRequiredContexts,
    ModelFloorRunner,
    ModelFloorRunnerFleet,
    ModelFloorStoppage,
    ModelFloorTableTask,
    ModelFloorWatcher,
    ModelLimitApiKey,
    ModelLimitEngine,
    ModelLimitLabRung,
    ModelLimitToken,
    ModelLimitTokenPool,
)
from omnimarket.models.model_floor_placement_reading import ModelFloorPlacementReading
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.plan_task_tickets import (
    merge_task_links,
    prose_task_tickets,
)
from omnimarket.nodes.node_factory_floor_effect.handlers.plan_task_tickets import (
    with_table_evidence as _with_table_evidence,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorContainerHost,
    ModelFactoryFloorContainerSource,
    ModelFactoryFloorEffectContract,
    ModelFactoryFloorPlanRef,
    ModelFactoryFloorTaskLinks,
)
from omnimarket.nodes.node_lane_container_budget_compute.handlers.handler_lane_container_budget import (
    HandlerLaneContainerBudget,
)

ROW_RE = re.compile(r"^(?:(\S+?):)?L(\d+) (.*)$")
TICKET_RE = re.compile(r"\bOMN-\d+\b")
PR_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.-]*#\d+\b")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
ID_KEY_RE = re.compile(
    r"^(?:lane|from|ticket|tickets|parent_ticket|pr|prs|id|re|until|task|state|subject|verdict|outcome|closes-claim|closes-CLAIM|host|model|actor|to|run|head|ruling|cited-rulings|mechanism-ticket)$"
)
STATUS_TYPES = {
    "in progress": "started",
    "in review": "started",
    "done": "completed",
    "canceled": "canceled",
    "cancelled": "canceled",
    "duplicate": "canceled",
}
CI_VERDICTS: dict[str, CiState] = {
    "GREEN": "green",
    "RED": "red",
    "PENDING": "pending",
    "NONE": "none",
}


class SourceRefusedError(ValueError):
    """A source cannot provide trustworthy facts until the caller repairs it."""


@contextmanager
def _source(source: Path | str) -> Iterator[None]:
    try:
        yield
    except SourceRefusedError:
        raise
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        yaml.YAMLError,
    ) as exc:
        raise SourceRefusedError(
            f"{source}: {exc}; restore a readable, valid source and rerun"
        ) from exc


def _utc(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp needs a UTC offset")
    return result.astimezone(UTC)


# The hour is 00..23: Python 3.14's fromisoformat reads T24:00:00 as the next midnight, strptime never did.
_TS_SHAPE = re.compile(r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):\d{2}:\d{2}Z", re.ASCII)


def _ledger_ts(value: str) -> datetime | None:
    """A ledger row's UTC stamp. A zero-padded ``TS_FMT`` stamp is read by the C parser (strptime is the cost of a 14-day
    read: one call per row, about 250,000 rows); anything else takes the strict parse and so keeps its old answer."""
    if _TS_SHAPE.fullmatch(value):
        try:
            return datetime.fromisoformat(value[:-1]).replace(tzinfo=UTC)
        except ValueError:
            return None
    try:
        return datetime.strptime(value, TS_FMT).replace(tzinfo=UTC)
    except ValueError:
        return None


def _tickets(*values: str) -> tuple[str, ...]:
    found = TICKET_RE.findall("\n".join(values).replace("omn-", "OMN-"))
    return tuple(sorted(set(found)))


def _csv(*values: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(part for value in values for part in value.split(",") if part)
    )


def load_contract(path: Path) -> ModelFactoryFloorEffectContract:
    """This node's own contract: the runtime block (sources, output path)."""
    with _source(path):
        return ModelFactoryFloorEffectContract.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )


def load_compute_config(path: Path) -> ModelFactoryFloorConfig:
    """The compute node's vocabularies and limits: the ``config`` block of its contract."""
    with _source(path):
        return ModelFactoryFloorConfig.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))["config"]
        )


OWN_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"


def contract_ledger_kinds() -> frozenset[str]:
    """The row kinds this node's own contract keeps whole (``ledger_kinds``)."""
    return frozenset(load_contract(OWN_CONTRACT_PATH).runtime.ledger_kinds)


def parse_ledger_text(
    text: str, kinds: frozenset[str] | None = None
) -> tuple[ModelFloorLedgerRow, ...]:
    """The rows of one reader answer. ``kinds`` (default: the contract's ``ledger_kinds``) are kept whole; of the other
    kinds only each lane's newest row is kept."""
    if kinds is None:
        kinds = contract_ledger_kinds()
    kept: list[tuple[int, ModelFloorLedgerRow]] = []
    latest: dict[str, tuple[int, ModelFloorLedgerRow]] = {}
    for order, line in enumerate(text.splitlines()):
        match = ROW_RE.match(line)
        if line.startswith("ROWS ") or match is None:
            continue
        cells = [cell.strip() for cell in match.group(3).split(" | ")]
        ts = _ledger_ts(cells[0])
        if ts is None or len(cells) < 2 or not cells[1]:
            continue
        fields: dict[str, str] = {}
        remainders: list[str] = []
        for cell in cells[2:]:
            key, sep, value = cell.partition("=")
            if sep and key == "parent":
                # A row written as `parent=lane-x Do the thing` carries its sentence after the first token.
                remainders.append(" ".join(value.split()[1:]))
            elif sep and ID_KEY_RE.fullmatch(key):
                tokens = value.split()
                fields.setdefault(key, tokens[0] if tokens else "")
                if len(tokens) > 1:
                    remainders.append(" ".join(tokens[1:]))
            elif sep and key == "owns":
                # An explicit owns= cell, the ledger MSG's routing rule: a comma list with no spaces in it.
                fields.setdefault("owns", value.split()[0] if value.split() else "")
            elif sep and key == "needs":
                # The closing row's typed ask (OMN-23417): `<action>:<target>`, or `unparsed`; one token, no spaces.
                fields.setdefault("needs", value.split()[0] if value.split() else "")
            elif sep and key == "report":
                # The lane's report text, one cell value written by the close node (OMN-23417).
                fields.setdefault("report", value.strip())
            elif sep and key in {"reason", "blocker"}:
                # A lane that ends blocked says why in blocker=; the sentence after its cells is the launcher's receipt note.
                fields.setdefault("reason", value.strip()[:300])
        free_text = (
            cells[-1]
            if not re.match(r"^[A-Za-z_-]+=", cells[-1])
            else max(remainders, key=len, default="")
        )
        row = ModelFloorLedgerRow(
            ts=ts,
            kind=cells[1],
            lane=fields.get("lane") or fields.get("from", ""),
            tickets=_csv(fields.get("ticket", ""), fields.get("tickets", "")),
            parent_tickets=_csv(fields.get("parent_ticket", "")),
            prs=_csv(fields.get("pr", ""), fields.get("prs", "")),
            row_id=fields.get("id", ""),
            re=fields.get("re", ""),
            until=_ledger_ts(fields.get("until", "")),
            reason=fields.get("reason") or free_text[:300],
            task=fields.get("task", ""),
            state=fields.get("state", ""),
            subject=fields.get("subject", ""),
            verdict=fields.get("verdict", ""),
            outcome=fields.get("outcome", ""),
            closes_claim=fields.get("closes-claim") or fields.get("closes-CLAIM", ""),
            text=free_text[:400],
            host=fields.get("host", ""),
            model=fields.get("model", ""),
            actor=fields.get("actor", ""),
            to=fields.get("to", ""),
            run_id=fields.get("run", ""),
            owns=_csv(fields.get("owns", "")),
            head=fields.get("head", ""),
            ruling=fields.get("ruling") or fields.get("cited-rulings", ""),
            mechanism=fields.get("mechanism-ticket", ""),
            needs=fields.get("needs", ""),
            report=fields.get("report", ""),
        )
        core = row.kind in kinds
        if core:
            kept.append((order, row))
        if (
            not core
            and row.lane
            and (
                row.lane not in latest
                or (ts, order) > (latest[row.lane][1].ts, latest[row.lane][0])
            )
        ):
            latest[row.lane] = (order, row)
    held = {order for order, _ in kept}
    ordered = sorted(
        [*kept, *(pair for pair in latest.values() if pair[0] not in held)],
        key=lambda pair: (pair[1].ts, pair[0]),
    )
    return tuple(row for _, row in ordered)


def _run_ledger_reader(
    reader: Path, query: list[str], timeout_seconds: int, kinds: frozenset[str]
) -> tuple[ModelFloorLedgerRow, ...]:
    with _source(reader):
        try:
            result = subprocess.run(
                [sys.executable, str(reader), *query],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceRefusedError(
                f"ledger reader {reader} did not finish within {timeout_seconds} s: repair the reader or supply ledger_rows_file"
            ) from exc
        if result.returncode:
            raise SourceRefusedError(
                f"ledger reader {reader} exited {result.returncode}: {result.stderr[:200]}; repair the reader or supply ledger_rows_file"
            )
        return parse_ledger_text(result.stdout, kinds)


def read_ledger_rows(
    *,
    window_days: int,
    cut_chars: int,
    timeout_seconds: int,
    plugin_root: Path | None,
    plugin_root_env: str,
    ledger_rows_file: Path | None,
    kinds: frozenset[str] | None = None,
) -> tuple[ModelFloorLedgerRow, ...]:
    kinds = contract_ledger_kinds() if kinds is None else kinds
    if ledger_rows_file is not None:
        with _source(ledger_rows_file):
            return parse_ledger_text(
                ledger_rows_file.read_text(encoding="utf-8"), kinds
            )
    if plugin_root is None:
        raise SourceRefusedError(
            f"{plugin_root_env} is not set: export the plugin root or supply ledger_rows_file"
        )
    reader = plugin_root / "scripts" / "onex_ledger.py"
    query = ["query", "--since", f"{window_days}d", "--archives"]
    query += ["--limit", "1000000", "--cut", str(cut_chars)]
    return _run_ledger_reader(reader, query, timeout_seconds, kinds)


def read_unresolved_rows(
    *,
    reads: tuple[tuple[str, str], ...],
    cut_chars: int,
    timeout_seconds: int,
    plugin_root: Path | None,
    plugin_root_env: str,
    stand_in_file: Path | None,
    kinds: frozenset[str],
    older_than: datetime,
) -> tuple[ModelFloorLedgerRow, ...]:
    """Rows of the live ledger and every archive roll, with no age limit, that the window read did not bring.

    Each ``(kind, text)`` read is one query of the same ledger reader over the live ledger and the archive rolls. The
    window's own rows are already in the request; only rows older than ``older_than`` are returned, and a row is
    returned once however many reads match it. Which of them a later row resolved is the compute's call.
    """
    if stand_in_file is not None:
        # A rows file stands in for the live ledger and the archive rolls (tests and replays): the same kind and text
        # filters the reader applies are applied to its lines.
        with _source(stand_in_file):
            lines = stand_in_file.read_text(encoding="utf-8").splitlines()
        wanted = [
            line
            for line in lines
            if any(f" | {kind} | " in line and grep in line for kind, grep in reads)
        ]
        rows = parse_ledger_text("\n".join(wanted), kinds | {kind for kind, _ in reads})
        return tuple(row for row in rows if row.ts < older_than)
    if plugin_root is None:
        raise SourceRefusedError(
            f"{plugin_root_env} is not set: export the plugin root or supply a rows file"
        )
    reader = plugin_root / "scripts" / "onex_ledger.py"
    found: dict[tuple[datetime, str, str], ModelFloorLedgerRow] = {}
    for kind, grep in reads:
        query = ["query", "--archives", "--kind", kind, "--grep", grep]
        query += ["--limit", "1000000", "--cut", str(cut_chars)]
        for row in _run_ledger_reader(
            reader, query, timeout_seconds, kinds | {kind for kind, _ in reads}
        ):
            found.setdefault((row.ts, row.kind, row.lane), row)
    return tuple(
        row
        for _, row in sorted(found.items(), key=lambda pair: pair[0])
        if row.ts < older_than
    )


def _ci_note(ci: dict[str, Any]) -> str:
    """``2 red, 1 pending of 152`` from the watcher's CI block; empty when it holds no counts."""
    parts = [
        f"{len(ci[key])} {label}"
        for key, label in (("red", "red"), ("pending", "pending"))
        if isinstance(ci.get(key), list) and ci[key]
    ]
    total = ci.get("total")
    if parts and isinstance(total, int):
        return f"{', '.join(parts)} of {total}"
    return ", ".join(parts)


def _contexts(ci: dict[str, Any]) -> tuple[ModelFloorContext, ...]:
    """Each check run of the head with its result: the conclusion once completed, else the status."""
    found: dict[str, str] = {}
    for run in ci.get("runs") or ():
        if not isinstance(run, list) or len(run) < 3 or not run[0]:
            continue
        name, status, conclusion = str(run[0]), str(run[1]), str(run[2])
        found[name] = conclusion if status == "completed" and conclusion else status
    return tuple(
        ModelFloorContext(name=name, result=result)
        for name, result in sorted(found.items())
        if result
    )


def load_watcher(path: Path, now: datetime, max_age_min: int) -> ModelFloorWatcher:
    with _source(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        tick = _utc(data["last_tick"])
        age = (now - tick).total_seconds() / 60
        if age > max_age_min:
            raise SourceRefusedError(
                f"watcher {path} is {age:.1f} minutes old (limit {max_age_min}): refresh the watcher or explicitly increase max_age_min"
            )
        prs: list[ModelFloorPr] = []
        for key, value in data.get("prs", {}).items():
            facts = value.get("facts", {})
            if facts.get("state") not in ("OPEN", "MERGED", "CLOSED"):
                continue
            cls = value.get("cls", "")
            ci = value.get("ci") or {}
            prs.append(
                ModelFloorPr(
                    key=key,
                    state=facts["state"],
                    draft=facts.get("draft", False),
                    red=cls == "red"
                    or "red" in cls.split("-")
                    or bool((value.get("ci") or {}).get("red")),
                    ci=CI_VERDICTS.get(str(ci.get("verdict")), "unread"),
                    ci_note=_ci_note(ci),
                    head_sha=str(facts.get("head_sha") or ci.get("sha") or ""),
                    contexts=_contexts(ci),
                    title=facts.get("title", ""),
                    head_ref=facts.get("head_ref", ""),
                    tickets=_tickets(facts.get("title", ""), facts.get("head_ref", "")),
                )
            )
        merges = tuple(
            ModelFloorMerge(
                key=key,
                merged_at=_utc(value["merged_at"]),
                title=value.get("title", ""),
                head_ref=value.get("head_ref", ""),
                tickets=_tickets(value.get("title", ""), value.get("head_ref", "")),
            )
            for key, value in sorted(data.get("merges", {}).items())
        )
        return ModelFloorWatcher(
            last_tick=tick, prs=tuple(sorted(prs, key=lambda pr: pr.key)), merges=merges
        )


def load_linear(path: Path | None) -> tuple[ModelFloorLinearIssue, ...]:
    if path is None:
        return ()
    with _source(path):
        if not path.exists():
            return ()
        text = path.read_text(encoding="utf-8")
        issues: list[ModelFloorLinearIssue] = []
        if not text.lstrip().startswith(("{", "[")):
            for line in text.splitlines():
                match = re.match(
                    r"^(OMN-\d+)\s+(.+?)\s+(\d{4}-\d\d-\d\dT[\d:.]+Z?)(?:\s+priority=(.*?))?\s*$",
                    line.strip(),
                )
                if match is None:
                    continue
                status = match.group(2)
                kind = STATUS_TYPES.get(status.lower(), "")
                issues.append(
                    ModelFloorLinearIssue(
                        id=match.group(1), status=status, status_type=kind
                    )
                )
        else:
            data = json.loads(text)
            for issue in data.get("issues", []) if isinstance(data, dict) else data:
                identifier = issue.get("id") or issue.get("identifier")
                if (
                    not isinstance(identifier, str)
                    or TICKET_RE.fullmatch(identifier) is None
                ):
                    continue
                issues.append(
                    ModelFloorLinearIssue(
                        id=identifier,
                        status=issue.get("status") or issue.get("state") or "",
                        status_type=issue.get("statusType")
                        or STATUS_TYPES.get(
                            str(
                                issue.get("status") or issue.get("state") or ""
                            ).lower(),
                            "",
                        ),
                        title=" ".join(str(issue.get("title") or "").split()),
                        children=tuple(
                            child
                            for child in (issue.get("children") or ())
                            if isinstance(child, str)
                            and TICKET_RE.fullmatch(child) is not None
                        ),
                    )
                )
        return tuple(sorted(issues, key=lambda issue: issue.id))


def merge_linear_titles(
    issues: tuple[ModelFloorLinearIssue, ...],
    titled: tuple[ModelFloorLinearIssue, ...],
) -> tuple[ModelFloorLinearIssue, ...]:
    """The status text file gives each issue its status type; the JSON snapshot gives its title (and any issue only it holds)."""
    titles = {issue.id: issue.title for issue in titled if issue.title}
    children = {issue.id: issue.children for issue in titled if issue.children}
    merged = {
        issue.id: issue.model_copy(
            update={
                "title": titles.get(issue.id, issue.title),
                "children": children.get(issue.id, issue.children),
            }
        )
        for issue in issues
    }
    for issue in titled:
        merged.setdefault(issue.id, issue)
    return tuple(sorted(merged.values(), key=lambda issue: issue.id))


def newest_linear_snapshot(directory: Path, glob: str) -> Path | None:
    with _source(directory):
        return max(
            (path for path in directory.glob(glob) if path.is_file()),
            key=lambda path: path.name,
            default=None,
        )


def newest_dated_file(directory: Path, filename: str) -> Path | None:
    with _source(directory):
        if not directory.exists():
            return None
        folders = (
            path
            for path in directory.iterdir()
            if path.name[0].isdigit() and path.is_dir() and (path / filename).is_file()
        )
        newest = max(folders, key=lambda path: path.name, default=None)
        return newest / filename if newest is not None else None


def read_placement(path: Path) -> ModelFloorPlacementReading:
    """The host advertisements, read tolerantly: a field the model does not declare is ignored and named, a row that
    still fails validation is skipped and named. The file being unreadable or not a list still refuses (OMN-23435)."""
    with _source(path):
        if not path.exists():
            return ModelFloorPlacementReading()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("expected a JSON list of host advertisements")
        declared = set(ModelHostCapacityAdvertisement.model_fields)
        hosts: list[ModelHostCapacityAdvertisement] = []
        unknown: list[str] = []
        unreadable: list[str] = []
        for index, value in enumerate(data):
            if not isinstance(value, dict):
                unreadable.append(f"row {index}: not a JSON object")
                continue
            name = str(value.get("host_name") or f"row {index}")
            unknown.extend(f"{name}.{key}" for key in sorted(set(value) - declared))
            try:
                hosts.append(
                    ModelHostCapacityAdvertisement.model_validate(
                        {key: val for key, val in value.items() if key in declared}
                    )
                )
            except ValidationError as exc:
                first = exc.errors()[0]
                unreadable.append(
                    f"{name}: {'.'.join(str(part) for part in first['loc'])} {first['msg']}"
                )
        return ModelFloorPlacementReading(
            hosts=tuple(sorted(hosts, key=lambda host: host.host_name)),
            unknown_fields=tuple(unknown),
            unreadable_rows=tuple(unreadable),
        )


def load_placement(path: Path) -> tuple[ModelHostCapacityAdvertisement, ...]:
    return read_placement(path).hosts


def load_stoppages(path: Path) -> tuple[ModelFloorStoppage, ...]:
    with _source(path):
        if not path.exists():
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
        return tuple(
            ModelFloorStoppage(
                name=value["class"],
                occurrences=value["occurrences"],
                cost_minutes=value["cost_minutes"],
                recurring=value.get("recurring", False),
                tickets=value.get("tickets", ()),
                last_seen=value.get("last_seen", ""),
                resolved_at=value.get("resolved_at", ""),
            )
            for value in data.get("classes", [])
            if "class" in value
        )


def header_text(text: str, keys: tuple[str, ...]) -> str:
    """The plan's header lines that start with one of ``keys`` (``key: value``), up to the first section heading."""
    wanted = {key.casefold() for key in keys}
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            break
        key, sep, _ = line.partition(":")
        if sep and key.strip().lstrip("-").strip(" *_").casefold() in wanted:
            lines.append(line)
    return "\n".join(lines)


def heading_patterns(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    """The contract's task heading patterns, compiled line by line; each must name the group ``id``."""
    compiled = tuple(re.compile(pattern, re.MULTILINE) for pattern in patterns)
    for pattern in compiled:
        if "id" not in pattern.groupindex:
            raise SourceRefusedError(
                f"heading task pattern {pattern.pattern!r} names no 'id' group: correct heading_task_patterns"
            )
    return compiled


SECTION_RE = re.compile(r"^## (.*)$", re.MULTILINE)
REVIEW_SECTION_RE = re.compile(r"adversarial|review|checklist", re.IGNORECASE)
TASK_SECTION_RE = re.compile(r"\btasks?\b", re.IGNORECASE)
SUBHEADING_TASK_RE = re.compile(
    r"^#{3,4}\s+([A-Z]{1,4}\.\d+(?:\.\d+)*[a-z]?)[.:]?\s+(.+?)\s*$", re.MULTILINE
)


def _sections(text: str) -> tuple[tuple[str, int, int], ...]:
    """Each ``## `` section of a plan as (heading, start, end)."""
    marks = list(SECTION_RE.finditer(text))
    return tuple(
        (
            mark.group(1),
            mark.start(),
            marks[i + 1].start() if i + 1 < len(marks) else len(text),
        )
        for i, mark in enumerate(marks)
    )


def _in_review_section(text: str, offset: int) -> bool:
    """True when ``offset`` falls in an adversarial-review or checklist section: its R1 to R8 are checks on the plan, not its tasks."""
    return any(
        start <= offset < end and REVIEW_SECTION_RE.search(heading) is not None
        for heading, start, end in _sections(text)
    )


def _only_in_review(label: str, text: str) -> bool:
    """True when every place the plan leads a line with the label (bold or a table cell) is in a review section."""
    leads = [
        match.start()
        for match in re.finditer(
            rf"^(?:\d+\.\s+|[-*]\s+)?(?:\*\*|\|\s*){re.escape(label)}(?![\w.])",
            text,
            re.MULTILINE,
        )
    ]
    return bool(leads) and all(_in_review_section(text, at) for at in leads)


def _heading_tasks(
    text: str, links: dict[str, tuple[str, ...]], epic: str
) -> tuple[ModelFloorManifestTask, ...]:
    """The tasks a plan writes as ``### CM.1 Title`` headings inside its own ``## Tasks`` section, which win over any bold list."""
    tasks: dict[str, ModelFloorManifestTask] = {}
    for heading, start, end in _sections(text):
        if (
            TASK_SECTION_RE.search(heading) is None
            or REVIEW_SECTION_RE.search(heading) is not None
        ):
            continue
        for match in SUBHEADING_TASK_RE.finditer(text, start, end):
            task_id = match.group(1)
            if task_id in tasks:
                continue
            title = match.group(2).strip(" .:")
            tasks[task_id] = ModelFloorManifestTask(
                id=task_id,
                title=f"{task_id} {title}"[:200],
                tickets=tuple(
                    dict.fromkeys(
                        (
                            *(
                                t
                                for t in sorted(set(TICKET_RE.findall(title)))
                                if t != epic
                            ),
                            *links.get(task_id, ()),
                        )
                    )
                ),
                prs=tuple(sorted(set(PR_RE.findall(title)))),
                scoped=True,
            )
    return tuple(tasks.values())


def parse_heading_tasks(
    text: str,
    links: dict[str, tuple[str, ...]],
    epic: str = "",
    *,
    patterns: tuple[re.Pattern[str], ...],
) -> tuple[ModelFloorManifestTask, ...]:
    """A plan's tasks from its bold-led task paragraphs (``**T1. Title.** ...``, ``1. **S1, now:** ...``), for a plan with no manifest and no table.

    The tickets and pull requests a task's lead names belong to the task (the plan's own ``epic`` ticket belongs to no
    one task), and ``links`` adds the tickets that carry a
    task whose paragraph names none. Only ids that start with the most common letter count, so a plan's decisions
    (``**D3.**``) are not drawn as tasks, and a repeated id keeps its first paragraph. The tasks are ``scoped``: a bare
    ``T1`` names a different task in every plan.
    """
    sectioned = _heading_tasks(text, links, epic)
    if sectioned:
        return sectioned
    found = next(
        (
            matches
            for pattern in patterns
            if (
                matches := [
                    match
                    for match in pattern.finditer(text)
                    if not _in_review_section(text, match.start())
                ]
            )
        ),
        [],
    )
    if not found:
        return ()
    letters = [match.group("id")[0] for match in found]
    wanted = max(set(letters), key=lambda letter: (letters.count(letter), letter))
    tasks: dict[str, ModelFloorManifestTask] = {}
    for match in found:
        groups = match.groupdict()
        task_id = match.group("id")
        if task_id[0] != wanted or task_id in tasks:
            continue
        lead, rest = groups.get("title") or "", groups.get("rest") or ""
        title = (lead or rest).strip(" .:")
        # A task's own lead names its tickets and pull requests; a list item's whole line is its lead, a paragraph's prose is not.
        named = lead + ("" if match.group(0).startswith("**") else f" {rest}")
        tasks[task_id] = ModelFloorManifestTask(
            id=task_id,
            title=f"{task_id} {title}"[:200],
            tickets=tuple(
                dict.fromkeys(
                    (
                        *(
                            t
                            for t in sorted(set(TICKET_RE.findall(named)))
                            if t != epic
                        ),
                        *links.get(task_id, ()),
                    )
                )
            ),
            prs=tuple(sorted(set(PR_RE.findall(named)))),
            scoped=True,
        )
    return tuple(tasks.values())


def _git_show(kb: Path, ref: str, relative: str) -> str | None:
    """A file as ``ref`` holds it, or None when the ref or the file is absent."""
    result = subprocess.run(
        ["git", "-C", str(kb), "show", f"{ref}:{relative}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def is_section_heading(label: str, text: str) -> bool:
    """True when the plan has a markdown heading that opens with the label (``### A.1 The pattern``)."""
    return (
        re.search(rf"^#{{1,6}}\s+{re.escape(label)}(?![\w.])", text, re.MULTILINE)
        is not None
    )


def _plan_input(
    *,
    relative: str,
    text: str,
    manifest: ModelFloorManifest | None,
    entry: dict[str, Any] | None,
    header_keys: tuple[str, ...],
    links: dict[str, tuple[str, ...]],
    patterns: tuple[re.Pattern[str], ...],
) -> ModelFloorPlanInput:
    header = header_text(text, header_keys)
    title = re.search(r"^# (.*)", text, re.MULTILINE)
    table = (
        tuple(ModelFloorTableTask.model_validate(task) for task in entry["tasks"])
        if entry is not None
        else ()
    )
    tickets = (
        tuple(entry.get("tickets", ()))
        if entry is not None
        else tuple(sorted(set(TICKET_RE.findall(header))))
    )
    # A review checklist is not a part: its R1 to R8 are checks on the plan, whatever table the plans-today writer found.
    table = tuple(row for row in table if not _only_in_review(row.label, text))
    # A section heading is not a part: the plans-today table lists a plan's ``### A.1`` headings beside its tasks.
    sections = {row.label for row in table if is_section_heading(row.label, text)}
    parts = tuple(row for row in table if row.label not in sections)
    if (manifest is None or not manifest.tasks) and (not table or sections):
        # Neither a manifest nor a table of parts: the plan's own task paragraphs are the line's tasks.
        header_tickets = TICKET_RE.findall(header)
        epic = header_tickets[0] if header_tickets else ""
        # The plan's own prose pairs a task id with its tickets (``T0.1 OMN-21494``); the overlay's links add to them.
        written = parse_heading_tasks(
            text,
            merge_task_links(prose_task_tickets(text, epic), links),
            epic=epic,
            patterns=patterns,
        )
        if written:
            by_label = {row.label: row for row in table}
            manifest = ModelFloorManifest(
                tasks=tuple(
                    _with_table_evidence(task, by_label.get(task.id))
                    for task in written
                )
            )
            tickets = tuple(sorted({*tickets, *header_tickets}))
            table = ()
        elif parts:
            table = parts
    return ModelFloorPlanInput(
        plan_path=relative,
        title=title.group(1).strip() if title else "",
        manifest=manifest,
        tickets=tickets,
        prs=tuple(entry.get("prs", ()))
        if entry is not None
        else tuple(sorted(set(PR_RE.findall(header)))),
        table_tasks=table,
    )


def load_plans(
    *,
    kb: Path,
    plans_relpath: str,
    manifest_suffix: str,
    header_keys: tuple[str, ...],
    heading_task_patterns: tuple[str, ...],
    contains: tuple[str, ...],
    plans_today: dict[str, Any] | None,
    plan_refs: tuple[ModelFactoryFloorPlanRef, ...] = (),
    task_links: tuple[ModelFactoryFloorTaskLinks, ...] = (),
) -> tuple[ModelFloorPlanInput, ...]:
    patterns = heading_patterns(heading_task_patterns)
    directory = kb / plans_relpath
    with _source(directory):
        if not directory.is_dir():
            raise SourceRefusedError(
                f"plans directory {directory} is missing: restore it or correct plans_relpath and kb"
            )
        paths = sorted(directory.rglob("*.md"))
    entries = {entry["path"]: entry for entry in (plans_today or {}).get("plans", [])}

    def links_of(relative: str) -> dict[str, tuple[str, ...]]:
        return {
            task: tickets
            for link in task_links
            if link.plan_path_contains in relative
            for task, tickets in link.tasks.items()
        }

    def wanted(relative: str) -> bool:
        return not contains or any(value in relative for value in contains)

    plans: list[ModelFloorPlanInput] = []
    for path in paths:
        relative = path.relative_to(kb).as_posix()
        if path.name.endswith(".readable.md") or not wanted(relative):
            continue
        with _source(path):
            text = path.read_text(encoding="utf-8")
            manifest_path = path.with_name(path.stem + manifest_suffix)
            manifest = None
            with _source(manifest_path):
                if manifest_path.exists():
                    manifest = ModelFloorManifest.model_validate(
                        yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
                    )
            plans.append(
                _plan_input(
                    relative=relative,
                    text=text,
                    manifest=manifest,
                    entry=entries.get(relative),
                    header_keys=header_keys,
                    links=links_of(relative),
                    patterns=patterns,
                )
            )
    drawn = {plan.plan_path for plan in plans}
    for plan_ref in plan_refs:
        # A plan on the default branch wins; the ref is read only until the plan merges.
        if plan_ref.path in drawn or not wanted(plan_ref.path):
            continue
        manifest_ref = plan_ref.path.removesuffix(".md") + manifest_suffix
        with _source(f"{plan_ref.ref}:{manifest_ref}"):
            ref_text = _git_show(kb, plan_ref.ref, plan_ref.path)
            if ref_text is None:
                continue
            manifest_text = _git_show(kb, plan_ref.ref, manifest_ref)
            plans.append(
                _plan_input(
                    relative=plan_ref.path,
                    text=ref_text,
                    manifest=ModelFloorManifest.model_validate(
                        yaml.safe_load(manifest_text)
                    )
                    if manifest_text is not None
                    else None,
                    entry=entries.get(plan_ref.path),
                    header_keys=header_keys,
                    links=links_of(plan_ref.path),
                    patterns=patterns,
                )
            )
            # The first readable ref of a plan draws it; a stale declared ref does not hide the pull request's head.
            drawn.add(plan_ref.path)
    return tuple(plans)


def resolve_home(
    environ: Mapping[str, str], home_env: str, override: str | None
) -> Path:
    value = override or environ.get(home_env, "")
    if not value.strip():
        raise SourceRefusedError(
            f"{home_env} is not set (fail fast, no default): export it or source session.env"
        )
    return Path(value)


def write_result(path: Path, document: dict[str, Any]) -> None:
    serialized = (
        json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(serialized)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


# --- Machine bays and live lanes (OMN-21733) -------------------------------------------------------------------------
# Each source below is optional: a file that is absent or cannot be parsed returns None, which the record draws as
# "unread", never as an empty list.


def _optional[T](read: Callable[[], T]) -> T | None:
    try:
        return read()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError):
        return None


def resolve_dir(environ: Mapping[str, str], env_name: str, fallback: str) -> Path:
    """The directory ``env_name`` names, else ``fallback`` under the user's home."""
    value = environ.get(env_name, "").strip()
    return Path(value) if value else Path.home() / fallback


def resolve_host_table(
    environ: Mapping[str, str], env_name: str, home: Path, relpath: str
) -> Path | None:
    """The shared lab host table: the file ``env_name`` names, else ``relpath`` under ``home`` when the floor overlay
    declares one, else the ``lab_hosts`` overlay found through the overlay roots. None when none supplies it."""
    value = environ.get(env_name, "").strip()
    if value:
        return Path(value)
    if relpath:
        return home / relpath
    return find_node_overlay("lab_hosts", environ)


def split_reading(text: str) -> list[str]:
    """Split a pool reading on the commas outside square brackets (the Claude pool cell holds commas)."""
    parts: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(text):
        if char == "[":
            depth += 1
        elif char == "]":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _number(value: str) -> float | None:
    match = re.match(r"^\s*(-?\d+(?:\.\d+)?)", value)
    return float(match.group(1)) if match else None


_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*$", re.IGNORECASE)
_TO_GB = {"": 1 / 1024**3, "K": 1 / 1024**2, "M": 1 / 1024, "G": 1.0, "T": 1024.0}


def _size_gb(text: str) -> float | None:
    """``123.2G`` as 123.2; None for ``-`` or anything that is not a size."""
    match = _SIZE.match(text)
    return float(match.group(1)) * _TO_GB[match.group(2).upper()] if match else None


def _disks(cells: dict[str, str]) -> tuple[ModelFloorPoolDisk, ...]:
    """Every ``disk_<name>=free/total/pct`` cell whose free and total sizes read; a cell that does not read is left out."""
    disks: list[ModelFloorPoolDisk] = []
    for key, value in cells.items():
        if not key.startswith("disk_"):
            continue
        free, _, rest = value.partition("/")
        total = rest.partition("/")[0]
        free_gb, total_gb = _size_gb(free), _size_gb(total)
        if free_gb is not None and total_gb:
            disks.append(
                ModelFloorPoolDisk(
                    name=key.removeprefix("disk_"), free_gb=free_gb, total_gb=total_gb
                )
            )
    return tuple(disks)


def _pressure(
    cells: dict[str, str],
) -> tuple[float | None, Literal["", "psi", "level"]]:
    """The Linux ``mempsi`` figure when the reading has one, else the macOS ``pressure`` level."""
    psi = _number(cells.get("mempsi", ""))
    if psi is not None:
        return psi, "psi"
    level = _number(cells.get("pressure", ""))
    return (level, "level") if level is not None else (None, "")


def parse_pool_reading(line: str, read_at: datetime) -> ModelFloorPoolReading:
    """One ``host:key=value,...`` reading of the landing placement's pool read (``Reading`` as the runner prints it)."""
    host, _, rest = line.partition(":")
    host = host.strip()
    if not host or not rest:
        raise ValueError(f"a pool reading needs `host:cells`: {line[:60]!r}")
    if rest.startswith("UNREADABLE"):
        return ModelFloorPoolReading(
            host=host,
            read_at=read_at,
            error=rest.removeprefix("UNREADABLE").strip("() ") or "unreadable",
        )
    cells: dict[str, str] = {}
    refusal = ""
    for part in split_reading(rest):
        key, sep, value = part.partition("=")
        if sep:
            cells.setdefault(key.strip(), value.strip())
        elif "REFUSED(" in part:
            refusal = part[part.index("(") + 1 :].rstrip(")")
    cores, cap, slots, lane_slots = (
        _number(cells.get("cores", "")),
        _number(cells.get("lane_cap", "") or cells.get("cap", "")),
        _number(cells.get("lane_slots", "") or cells.get("slots", "")),
        _number(cells.get("lane_slots", "")),
    )
    pressure, pressure_kind = _pressure(cells)
    return ModelFloorPoolReading(
        host=host,
        read_at=read_at,
        disks=_disks(cells),
        mem_pressure=pressure,
        mem_pressure_kind=pressure_kind,
        cores=int(cores) if cores is not None else None,
        load1=_number(cells.get("load1", "")),
        busy_cores=_number(cells.get("busy", "")),
        mem_free_gb=_number(cells.get("free_gb", "")),
        mem_total_gb=_number(cells.get("memtotal", "")),
        swap_used_pct=_number(cells.get("swap", "")),
        cap=int(cap) if cap is not None else None,
        slots=int(slots) if slots is not None else None,
        lane_slots=int(lane_slots) if lane_slots is not None else None,
        refusal=refusal,
    )


def load_pool(path: Path) -> tuple[ModelFloorPoolReading, ...] | None:
    def read() -> tuple[ModelFloorPoolReading, ...]:
        data = json.loads(path.read_text(encoding="utf-8"))
        read_at = _utc(data["started_at"])
        readings = data["readings"]
        if not isinstance(readings, list):
            raise ValueError("readings is not a list")
        return tuple(
            parse_pool_reading(line, read_at)
            for line in readings
            if isinstance(line, str)
        )

    return _optional(read)


def load_host_drift(path: Path) -> ModelFloorHostDrift | None:
    """The host-drift fleet projection (``fleet.json``): when it was folded, and per host its newest heartbeat and its open items."""

    def read() -> ModelFloorHostDrift:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ModelFloorHostDrift(
            folded_at=_utc(data["folded_at"]),
            hosts=tuple(
                ModelFloorDriftHost(
                    host=str(row["host"]),
                    last_heartbeat_at=_utc(row["last_heartbeat_at"])
                    if row.get("last_heartbeat_at")
                    else None,
                    open_items=len(row.get("open") or ()),
                )
                for row in data["hosts"]
            ),
        )

    return _optional(read)


def host_drift_cause(path: Path) -> str:
    """Why the fleet projection is unread, in words the Machines tab can show; empty when it reads."""
    if not path.exists():
        return f"the host-drift fleet projection {path.name} does not exist at the declared path"
    return f"the host-drift fleet projection {path.name} exists but could not be parsed"


def load_host_table(path: Path) -> tuple[ModelFloorHostRow, ...] | None:
    def read() -> tuple[ModelFloorHostRow, ...]:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return tuple(
            ModelFloorHostRow(
                host=str(row["name"]),
                cap=row.get("cap"),
                available=row.get("available") is not False,
                unavailable_reason=str(row.get("unavailable_reason") or ""),
            )
            for row in data["hosts"]
        )

    return _optional(read)


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (ProcessLookupError, OverflowError, OSError):
        return False
    return True


def _runner_alive(pid: int) -> bool | None:
    """Only ESRCH proves an exited runner; permission and transport errors do not prove death."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return None
    return True


def load_placed(
    directory: Path, lane_min_id: int
) -> tuple[ModelFloorPlacedHost, ...] | None:
    """Live placement markers per host, read-only: ``<host>.<lease>.<pid>`` whose pid is alive."""

    def read() -> tuple[ModelFloorPlacedHost, ...]:
        units: dict[str, int] = {}
        lanes: dict[str, int] = {}
        for marker in directory.iterdir():
            parts = marker.name.rsplit(".", 2)
            if (
                marker.name.startswith(".")
                or len(parts) != 3
                or not parts[1].isdigit()
                or not parts[2].isdigit()
                or not _pid_alive(int(parts[2]))
            ):
                continue
            units[parts[0]] = units.get(parts[0], 0) + 1
            if int(parts[1]) >= lane_min_id:
                lanes[parts[0]] = lanes.get(parts[0], 0) + 1
        return tuple(
            ModelFloorPlacedHost(host=host, units=units[host], lanes=lanes.get(host, 0))
            for host in sorted(units)
        )

    return _optional(read)


def _output_written_at(receipt_path: Path) -> datetime | None:
    """When the runner last wrote the lane's streamed output (the ``.out`` beside the receipt); None when absent or unread."""
    try:
        stamp = receipt_path.with_suffix(".out").stat().st_mtime
        return datetime.fromtimestamp(stamp, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


def _receipt_of(path: Path, result_chars: int) -> ModelFloorReceipt | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cloud = _mapping(data.get("cloud"))
        state = _mapping(cloud.get("session_state"))
        ended = data.get("ended_at")
        pid = data.get("pid")
        return ModelFloorReceipt(
            lane=str(data["lane"]),
            run_id=str(data.get("run_id") or ""),
            host=str(data.get("host") or ""),
            status=str(data.get("status") or ""),
            final=bool(data.get("final")),
            started_at=_utc(data["started_at"]),
            ended_at=_utc(ended) if isinstance(ended, str) else None,
            runner_alive=_runner_alive(pid) if type(pid) is int and pid > 0 else None,
            output_at=_output_written_at(path),
            model=str(data.get("model") or ""),
            ticket=str(data.get("ticket") or ""),
            placement=str(data.get("placement") or ""),
            account=str(cloud.get("account") or ""),
            login=str(data.get("login") or ""),
            pr_heads=tuple(
                str(h) for h in data.get("pr_heads") or () if isinstance(h, str)
            ),
            result=" ".join(str(data.get("result") or "").split())[:result_chars],
            session_detail=" ".join(str(state.get("detail") or "").split())[
                :result_chars
            ],
            session_needs=" ".join(str(state.get("needs_action") or "").split())[
                :result_chars
            ],
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def load_receipts(
    directory: Path,
    *,
    now: datetime,
    window_hours: int,
    glob: str,
    result_chars: int,
    scan_workers: int,
) -> tuple[ModelFloorReceipt, ...] | None:
    """The newest receipt file of each lane directory touched inside the window (the directory holds thousands).

    Whether a directory was touched inside the window is one metadata read per directory, tens of thousands of them,
    and each waits on the disk when the process runs in the Background class; they are read ``scan_workers`` at a time.
    """

    def touched(entry: os.DirEntry[str]) -> bool:
        return entry.is_dir() and entry.stat().st_mtime >= since

    def newest_receipt(entry: os.DirEntry[str]) -> ModelFloorReceipt | None:
        files = [
            p for p in Path(entry.path).glob(glob) if not p.name.endswith(".job.json")
        ]
        newest = max(files, key=lambda p: p.stat().st_mtime, default=None)
        return _receipt_of(newest, result_chars) if newest else None

    since = now.timestamp() - window_hours * 3600

    def read() -> tuple[ModelFloorReceipt, ...]:
        with os.scandir(directory) as scanned:
            entries = list(scanned)
        with ThreadPoolExecutor(max_workers=scan_workers) as pool:
            recent = [
                e
                for e, keep in zip(entries, pool.map(touched, entries), strict=True)
                if keep
            ]
            found = [r for r in pool.map(newest_receipt, recent) if r is not None]
        return tuple(sorted(found, key=lambda r: (r.started_at, r.lane)))

    return _optional(read)


def load_lane_processes(
    ledger: tuple[ModelFloorLedgerRow, ...],
    *,
    directory: Path,
    plugin_root: Path | None,
    now: datetime,
    grace_seconds: int,
    window_minutes: int = 180,
) -> tuple[ModelFloorLaneProcess, ...] | None:
    """Reuse the controller's group/tag probe; never signal workers or infer death from ledger silence.

    These leases and the process table belong to the controller host, just like the placement markers.
    An unread directory, lease or table stays unknown. A readable directory with no lease or worker tag
    proves absence after the claim-before-spawn grace period.
    """
    claims: dict[str, ModelFloorLedgerRow] = {}
    terminals: dict[str, datetime] = {}
    newest: dict[str, datetime] = {}
    for row in ledger:
        newest[row.lane] = max(newest.get(row.lane, row.ts), row.ts)
        if row.kind == "TERMINAL":
            terminals[row.lane] = max(terminals.get(row.lane, row.ts), row.ts)
        if (
            row.kind == "CLAIM"
            and re.fullmatch(r"landing-(?:cause-)?L\d+", row.lane)
            and (row.lane not in claims or row.ts > claims[row.lane].ts)
        ):
            claims[row.lane] = row
    claims = {
        lane: claim
        for lane, claim in claims.items()
        if (lane not in terminals or terminals[lane] < claim.ts)
        and (now - newest[lane]).total_seconds() <= window_minutes * 60
    }
    if not claims:
        return ()
    if plugin_root is None:
        return None
    try:
        # A readable worker directory is the positive control for a missing lease file.
        # One directory read, no stat per entry: the directory holds tens of thousands of lease directories, and a stat of
        # each took more than 5 minutes as the publisher's tick (OMN-22699); a DirEntry knows its type from the read.
        with os.scandir(directory / "workers") as scanned:
            worker_ids = {e.name for e in scanned if e.is_dir()}
        workers = runpy.run_path(
            str(plugin_root / "skills/merge-drain/scripts/landing_workers.py")
        )
        table = workers["read_process_table"]()
        if table is None:
            return None
        found: list[ModelFloorLaneProcess] = []
        for lane, claim in claims.items():
            if (now - claim.ts).total_seconds() < grace_seconds:
                continue
            lease_id = int(lane.rsplit("L", 1)[1])
            lease = workers["read_lease"](directory, lease_id)
            if lease is None:
                if f"{lease_id:06d}" in worker_ids:
                    # A present but unreadable lease cannot be treated as a missing worker.
                    continue
                alive = any(
                    tag.startswith(f"landing-lease-{lease_id}-")
                    and not p.stat.startswith("Z")
                    for p in table
                    for tag in p.tags
                )
            else:
                # The lane's lease number is unique; claim time identifies the observation's ownership.
                group, tagged = workers["probe"](lease, table)
                alive = bool(group or tagged)
            found.append(
                ModelFloorLaneProcess(
                    lane=lane,
                    started_at=claim.ts,
                    observed_at=now,
                    alive=alive,
                    detail="controller worker group or tag is live"
                    if alive
                    else "controller worker group and tag are absent",
                )
            )
        return tuple(found)
    except (OSError, ValueError, KeyError, TypeError, ImportError):
        return None


def load_cloud_accounts(path: Path) -> tuple[ModelFloorCloudAccount, ...] | None:
    def read() -> tuple[ModelFloorCloudAccount, ...]:
        accounts = json.loads(path.read_text(encoding="utf-8"))["accounts"]
        out: list[ModelFloorCloudAccount] = []
        for name, value in sorted(accounts.items()):
            credit = _mapping(value.get("credit"))
            rate = _mapping(value.get("rate"))
            five_hour = _mapping(rate.get("five_hour"))
            seven_day = _mapping(rate.get("seven_day"))
            remaining = credit.get("remaining")
            cap = value.get("cap")
            out.append(
                ModelFloorCloudAccount(
                    name=name,
                    login=str(value.get("login") or ""),
                    cap=cap if isinstance(cap, int) else None,
                    active=tuple(sorted(value.get("active") or ())),
                    credit_remaining=float(remaining)
                    if isinstance(remaining, (int, float))
                    else None,
                    credit_read_at=str(credit.get("read_at") or ""),
                    rate_status=str(rate.get("status") or ""),
                    rate_resets_at=str(rate.get("resets_at") or ""),
                    rate_read_at=str(rate.get("read_at") or ""),
                    five_hour_utilization=_limit_number(five_hour.get("utilization")),
                    five_hour_resets_at=str(five_hour.get("resets_at") or ""),
                    seven_day_utilization=_limit_number(seven_day.get("utilization")),
                    seven_day_resets_at=str(seven_day.get("resets_at") or ""),
                    credit_source=str(credit.get("source") or ""),
                )
            )
        return tuple(out)

    return _optional(read)


def _limit_number(value: object) -> float | None:
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )


def load_api_keys(path: Path) -> tuple[ModelLimitApiKey, ...] | None:
    """Read monthly API spend without turning a missing reading into zero (OMN-22010)."""

    def read() -> tuple[ModelLimitApiKey, ...]:
        keys = json.loads(path.read_text(encoding="utf-8"))["keys"]
        return tuple(
            ModelLimitApiKey(
                name=name,
                month_spend_usd=_limit_number(value.get("month_spend_usd")),
                read_at=str(value.get("read_at") or ""),
                source=str(value.get("source", path.name)),
                budget_usd=_limit_number(value.get("budget_usd")),
            )
            for name, value in sorted(keys.items())
        )

    return _optional(read)


def load_engines(
    path: Path,
) -> tuple[
    ModelLimitEngine | None,
    ModelLimitEngine | None,
    tuple[ModelLimitLabRung, ...] | None,
]:
    """Read subscription windows and lab smoke results, preserving unread sources."""

    def read() -> tuple[
        ModelLimitEngine | None,
        ModelLimitEngine | None,
        tuple[ModelLimitLabRung, ...] | None,
    ]:
        data = json.loads(path.read_text(encoding="utf-8"))

        def engine(key: str) -> ModelLimitEngine | None:
            if key not in data:
                return None
            value = data[key]
            return ModelLimitEngine(
                used_pct=_limit_number(value.get("used_pct")),
                window=str(value.get("window") or ""),
                resets_at=str(value.get("resets_at") or ""),
                read_at=str(value.get("read_at") or ""),
                source=str(value.get("source", path.name)),
            )

        rungs = (
            tuple(
                ModelLimitLabRung(
                    name=name,
                    available=value.get("available"),
                    last_smoke_at=str(value.get("last_smoke_at") or ""),
                    last_smoke_ok=value.get("last_smoke_ok"),
                    read_at=str(value.get("read_at") or ""),
                    source=str(value.get("source", path.name)),
                )
                for name, value in sorted(data["lab_rungs"].items())
            )
            if "lab_rungs" in data
            else None
        )
        return engine("codex"), engine("glm"), rungs

    return _optional(read) or (None, None, None)


def load_token_pools(
    directory: Path, pattern: str
) -> tuple[ModelLimitTokenPool, ...] | None:
    """Read only host, timestamp and token status fields from each pool's mirror."""

    def pool(path: Path) -> ModelLimitTokenPool:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ModelLimitTokenPool(
            host=data["host"],
            read_at=str(data.get("at") or ""),
            source=path.name,
            tokens=tuple(
                ModelLimitToken(
                    label=token["label"],
                    limited_until=token.get("until") or "",
                    revoked=token.get("revoked", False),
                )
                for token in data["tokens"]
            ),
        )

    def read() -> tuple[ModelLimitTokenPool, ...] | None:
        pools = tuple(
            value
            for path in sorted(directory.glob(pattern))
            if (value := _optional(partial(pool, path))) is not None
        )
        return pools or None

    return _optional(read)


def load_runner_fleet(path: Path) -> ModelFloorRunnerFleet | None:
    """The fleet observation event (``scripts/runner_fleet_event.py``) reduced to each runner's host label and status."""

    return _optional(lambda: load_runner_fleet_strict(path))


def load_runner_fleet_strict(path: Path) -> ModelFloorRunnerFleet:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("status") == "unread":
        raise SourceRefusedError(str(data.get("reason") or "fleet producer unread"))
    return ModelFloorRunnerFleet(
        observed_at=_utc(data["observed_at"]),
        runners=tuple(
            ModelFloorRunner(
                host_label=str(row["host"]),
                status=row["status"],
                runner_name=row.get("runner_name"),
                label_class=row.get("label_class"),
                labels=tuple(row.get("labels") or ()),
            )
            for row in data["runners"]
        ),
    )


def runner_fleet_cause(path: Path) -> str:
    """Why the runner fleet observation at ``path`` is unread, naming the file; empty when it reads."""
    if not path.exists():
        return (
            f"the runner fleet file {path.name} does not exist: node_projection_runner_fleet saves the runner monitor's "
            "observation there, and none has been saved on this host"
        )
    try:
        load_runner_fleet_strict(path)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        return f"the runner fleet file {path.name} cannot be read: {type(exc).__name__}: {str(exc)[:120]}"
    return ""


# --- Lane containers (application container budget) ---------------------------------------------------------------------


def read_container_listing(
    source: ModelFactoryFloorContainerSource, host: ModelFactoryFloorContainerHost
) -> tuple[str, str]:
    """The host's container listing and, when it could not be read, why: ``(listing, error)``."""
    argv = ["ssh", *source.ssh_options, host.ssh_target, source.listing_command]
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=source.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return (
            "",
            f"the listing on {host.host} timed out after {source.timeout_seconds}s",
        )
    except OSError as exc:
        return "", f"the listing on {host.host} could not start: {exc}"
    if done.returncode:
        tail = " ".join(done.stderr.split())[-160:]
        return "", f"the listing on {host.host} exited {done.returncode}: {tail}"
    return _without_nameless_dead(done.stdout), ""


def _without_nameless_dead(listing: str) -> str:
    """Drop the rows docker lists for a removal that did not finish: state dead, no name.

    The budget node refuses a whole listing that holds a row with no name, so one such ghost row turned
    every lane of the host into UNKNOWN. A nameless row in any other state is kept and still refuses.
    """
    kept: list[str] = []
    for line in listing.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        if (
            isinstance(row, dict)
            and not str(row.get("Names") or "").strip()
            and row.get("State") == "dead"
        ):
            continue
        kept.append(line)
    return "\n".join(kept)


def load_lane_containers(
    *,
    source: ModelFactoryFloorContainerSource,
    hosts: tuple[ModelFactoryFloorContainerHost, ...],
    declarations: tuple[ModelLaneContainerBudgetDeclaration, ...],
    read: Callable[
        [ModelFactoryFloorContainerSource, ModelFactoryFloorContainerHost],
        tuple[str, str],
    ] = read_container_listing,
) -> tuple[ModelLaneContainerBudgetVerdict, ...] | None:
    """One budget verdict per declared host; None when the overlay declares no host (the containers are not read at all)."""
    if not hosts:
        return None
    judge = HandlerLaneContainerBudget()
    verdicts: list[ModelLaneContainerBudgetVerdict] = []
    for host in hosts:
        listing, error = read(source, host)
        verdicts.append(
            judge.handle(
                ModelLaneContainerBudgetRequest(
                    host=host.host,
                    docker_ps=listing,
                    read_error=error,
                    declarations=declarations,
                )
            )
        )
    return tuple(verdicts)


def load_required_contexts(path: Path) -> tuple[ModelFloorRequiredContexts, ...] | None:
    """The queue-status tick's saved map; None when the file is absent or unreadable (the caller records why).

    A key is ``repository`` or ``repository:branch``; a value is the context list, or an object with ``contexts``
    and ``read_at``.
    """
    with _source(path):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("required-contexts map is not an object")
        found: list[ModelFloorRequiredContexts] = []
        for key, value in sorted(data.items()):
            names = value.get("contexts") if isinstance(value, dict) else value
            read_at = value.get("read_at") if isinstance(value, dict) else None
            if not isinstance(names, list):
                raise ValueError(f"required contexts of {key} are not a list")
            found.append(
                ModelFloorRequiredContexts(
                    repo=key.partition(":")[0],
                    contexts=tuple(str(name) for name in names),
                    read_at=_utc(read_at) if isinstance(read_at, str) else None,
                )
            )
        return tuple(found)


def load_goal_rows(
    path: Path, closed_verdicts: tuple[str, ...]
) -> tuple[ModelFloorGoalRow, ...]:
    """The open rows of the goal file: ``id | what | verdict | next action | probe``. Header lines (``key: value``) and
    ``->`` sub-rows are not rows; a row whose verdict is in ``closed_verdicts`` is not open."""
    with _source(path):
        text = path.read_text(encoding="utf-8")
        rows: list[ModelFloorGoalRow] = []
        for line in text.splitlines():
            cells = [cell.strip() for cell in line.split(" | ")]
            if line.startswith(("->", "Format:")) or len(cells) < 3 or not cells[0]:
                continue
            if " " in cells[0] or cells[2].upper() in closed_verdicts:
                continue
            rows.append(
                ModelFloorGoalRow(
                    row_id=cells[0],
                    what=cells[1],
                    verdict=cells[2],
                    next_action=cells[3] if len(cells) > 3 else "",
                    probe=cells[4] if len(cells) > 4 else "",
                )
            )
        return tuple(rows)
