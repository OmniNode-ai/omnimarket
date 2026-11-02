# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read external sources into the factory floor's typed input facts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_factory_floor_input import (
    CiState,
    ModelFactoryFloorConfig,
    ModelFloorCloudAccount,
    ModelFloorHostRow,
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorManifest,
    ModelFloorManifestTask,
    ModelFloorMerge,
    ModelFloorPlacedHost,
    ModelFloorPlanInput,
    ModelFloorPoolReading,
    ModelFloorPr,
    ModelFloorReceipt,
    ModelFloorRunner,
    ModelFloorRunnerFleet,
    ModelFloorStoppage,
    ModelFloorTableTask,
    ModelFloorWatcher,
)
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
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
    r"^(?:lane|from|ticket|tickets|pr|prs|id|re|until|task|state|subject|verdict|outcome|closes-claim|closes-CLAIM|host|model|actor|to)$"
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


def _ledger_ts(value: str) -> datetime | None:
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


def parse_ledger_text(text: str) -> tuple[ModelFloorLedgerRow, ...]:
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
            elif sep and key == "reason":
                fields.setdefault(key, value.strip()[:300])
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
        )
        core = row.kind in {"CLAIM", "TERMINAL", "HOLD", "RELEASE", "STATUS"}
        # Every ask to a recipient and every row that answers one: an ask stays open until an answer names it.
        if core or (row.kind == "MSG" and row.to) or (row.kind == "ACK" and row.re):
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


def read_ledger_rows(
    *,
    window_days: int,
    cut_chars: int,
    plugin_root: Path | None,
    plugin_root_env: str,
    ledger_rows_file: Path | None,
) -> tuple[ModelFloorLedgerRow, ...]:
    if ledger_rows_file is not None:
        with _source(ledger_rows_file):
            return parse_ledger_text(ledger_rows_file.read_text(encoding="utf-8"))
    if plugin_root is None:
        raise SourceRefusedError(
            f"{plugin_root_env} is not set: export the plugin root or supply ledger_rows_file"
        )
    reader = plugin_root / "scripts" / "onex_ledger.py"
    with _source(reader):
        query = ["query", "--since", f"{window_days}d", "--archives"]
        query += ["--limit", "1000000", "--cut", str(cut_chars)]
        try:
            result = subprocess.run(
                [sys.executable, str(reader), *query],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SourceRefusedError(
                f"ledger reader {reader} timed out: repair the reader or supply ledger_rows_file"
            ) from exc
        if result.returncode:
            raise SourceRefusedError(
                f"ledger reader {reader} exited {result.returncode}: {result.stderr[:200]}; repair the reader or supply ledger_rows_file"
            )
        return parse_ledger_text(result.stdout)


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
                    title=facts.get("title", ""),
                    head_ref=facts.get("head_ref", ""),
                    tickets=_tickets(
                        facts.get("title", ""),
                        facts.get("head_ref", ""),
                        facts.get("body", ""),
                    ),
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
                    )
                )
        return tuple(sorted(issues, key=lambda issue: issue.id))


def merge_linear_titles(
    issues: tuple[ModelFloorLinearIssue, ...],
    titled: tuple[ModelFloorLinearIssue, ...],
) -> tuple[ModelFloorLinearIssue, ...]:
    """The status text file gives each issue its status type; the JSON snapshot gives its title (and any issue only it holds)."""
    titles = {issue.id: issue.title for issue in titled if issue.title}
    merged = {
        issue.id: issue.model_copy(update={"title": titles.get(issue.id, issue.title)})
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


def load_placement(path: Path) -> tuple[ModelHostCapacityAdvertisement, ...]:
    with _source(path):
        if not path.exists():
            return ()
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("expected a JSON list of host advertisements")
        hosts = (ModelHostCapacityAdvertisement.model_validate(value) for value in data)
        return tuple(sorted(hosts, key=lambda host: host.host_name))


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


HEADING_TASK_RE = re.compile(
    r"^(?:\d+\.\s+|[-*]\s+)?\*\*([A-Z]{1,4}\.?\d+(?:\.\d+)*[a-z]?)[.,:]?(?:\s+(.+?))?\*\*(.*)$",
    re.MULTILINE,
)


def parse_heading_tasks(
    text: str, links: dict[str, tuple[str, ...]], epic: str = ""
) -> tuple[ModelFloorManifestTask, ...]:
    """A plan's tasks from its bold-led task paragraphs (``**T1. Title.** ...``, ``1. **S1, now:** ...``), for a plan with no manifest and no table.

    The tickets and pull requests a task's lead names belong to the task (the plan's own ``epic`` ticket belongs to no
    one task), and ``links`` adds the tickets that carry a
    task whose paragraph names none. Only ids that start with the most common letter count, so a plan's decisions
    (``**D3.**``) are not drawn as tasks, and a repeated id keeps its first paragraph. The tasks are ``scoped``: a bare
    ``T1`` names a different task in every plan.
    """
    found = list(HEADING_TASK_RE.finditer(text))
    if not found:
        return ()
    letters = [match.group(1)[0] for match in found]
    wanted = max(set(letters), key=lambda letter: (letters.count(letter), letter))
    tasks: dict[str, ModelFloorManifestTask] = {}
    for match in found:
        task_id = match.group(1)
        if task_id[0] != wanted or task_id in tasks:
            continue
        title = (match.group(2) or match.group(3)).strip(" .:")
        # A task's own lead names its tickets and pull requests; a list item's whole line is its lead, a paragraph's prose is not.
        named = (match.group(2) or "") + (
            "" if match.group(0).startswith("**") else f" {match.group(3)}"
        )
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


def _plan_input(
    *,
    relative: str,
    text: str,
    manifest: ModelFloorManifest | None,
    entry: dict[str, Any] | None,
    header_keys: tuple[str, ...],
    links: dict[str, tuple[str, ...]],
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
    if (manifest is None or not manifest.tasks) and not table:
        # Neither a manifest nor a plans-today table: the plan's own task paragraphs are the line's tasks.
        header_tickets = TICKET_RE.findall(header)
        written = parse_heading_tasks(
            text, links, epic=header_tickets[0] if header_tickets else ""
        )
        if written:
            manifest = ModelFloorManifest(tasks=written)
            tickets = tuple(sorted({*tickets, *header_tickets}))
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
    contains: tuple[str, ...],
    plans_today: dict[str, Any] | None,
    plan_refs: tuple[ModelFactoryFloorPlanRef, ...] = (),
    task_links: tuple[ModelFactoryFloorTaskLinks, ...] = (),
) -> tuple[ModelFloorPlanInput, ...]:
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
                )
            )
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
    cores, cap, slots = (
        _number(cells.get("cores", "")),
        _number(cells.get("lane_cap", "") or cells.get("cap", "")),
        _number(cells.get("lane_slots", "") or cells.get("slots", "")),
    )
    return ModelFloorPoolReading(
        host=host,
        read_at=read_at,
        cores=int(cores) if cores is not None else None,
        load1=_number(cells.get("load1", "")),
        busy_cores=_number(cells.get("busy", "")),
        mem_free_gb=_number(cells.get("free_gb", "")),
        mem_total_gb=_number(cells.get("memtotal", "")),
        swap_used_pct=_number(cells.get("swap", "")),
        cap=int(cap) if cap is not None else None,
        slots=int(slots) if slots is not None else None,
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


def _receipt_of(path: Path, result_chars: int) -> ModelFloorReceipt | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cloud = _mapping(data.get("cloud"))
        state = _mapping(cloud.get("session_state"))
        ended = data.get("ended_at")
        return ModelFloorReceipt(
            lane=str(data["lane"]),
            run_id=str(data.get("run_id") or ""),
            host=str(data.get("host") or ""),
            status=str(data.get("status") or ""),
            final=bool(data.get("final")),
            started_at=_utc(data["started_at"]),
            ended_at=_utc(ended) if isinstance(ended, str) else None,
            model=str(data.get("model") or ""),
            ticket=str(data.get("ticket") or ""),
            placement=str(data.get("placement") or ""),
            account=str(cloud.get("account") or ""),
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
    directory: Path, *, now: datetime, window_hours: int, glob: str, result_chars: int
) -> tuple[ModelFloorReceipt, ...] | None:
    """The newest receipt file of each lane directory touched inside the window (the directory holds thousands)."""

    def read() -> tuple[ModelFloorReceipt, ...]:
        since = now.timestamp() - window_hours * 3600
        found: list[ModelFloorReceipt] = []
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.is_dir() or entry.stat().st_mtime < since:
                    continue
                files = [
                    p
                    for p in Path(entry.path).glob(glob)
                    if not p.name.endswith(".job.json")
                ]
                newest = max(files, key=lambda p: p.stat().st_mtime, default=None)
                receipt = _receipt_of(newest, result_chars) if newest else None
                if receipt is not None:
                    found.append(receipt)
        return tuple(sorted(found, key=lambda r: (r.started_at, r.lane)))

    return _optional(read)


def load_cloud_accounts(path: Path) -> tuple[ModelFloorCloudAccount, ...] | None:
    def read() -> tuple[ModelFloorCloudAccount, ...]:
        accounts = json.loads(path.read_text(encoding="utf-8"))["accounts"]
        out: list[ModelFloorCloudAccount] = []
        for name, value in sorted(accounts.items()):
            credit = _mapping(value.get("credit"))
            rate = _mapping(value.get("rate"))
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
                )
            )
        return tuple(out)

    return _optional(read)


def load_runner_fleet(path: Path) -> ModelFloorRunnerFleet | None:
    """The fleet observation event (``scripts/runner_fleet_event.py``) reduced to each runner's host label and status."""

    return _optional(lambda: load_runner_fleet_strict(path))


def load_runner_fleet_strict(path: Path) -> ModelFloorRunnerFleet:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ModelFloorRunnerFleet(
        observed_at=_utc(data["observed_at"]),
        runners=tuple(
            ModelFloorRunner(host_label=str(row["host"]), status=row["status"])
            for row in data["runners"]
        ),
    )


def runner_fleet_cause(path: Path) -> str:
    """Why the runner fleet observation at ``path`` is unread, naming the file; empty when it reads."""
    if not path.exists():
        return (
            f"the runner fleet file {path.name} does not exist: the runner monitor publishes the observation "
            "to the bus and no node saves it to a file this record reads"
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
