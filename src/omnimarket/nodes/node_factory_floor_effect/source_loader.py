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
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_factory_floor_input import (
    ModelFactoryFloorConfig,
    ModelFloorLedgerRow,
    ModelFloorLinearIssue,
    ModelFloorManifest,
    ModelFloorMerge,
    ModelFloorPlanInput,
    ModelFloorPr,
    ModelFloorStoppage,
    ModelFloorTableTask,
    ModelFloorWatcher,
)
from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.nodes.node_factory_floor_effect.models.model_factory_floor_runtime import (
    ModelFactoryFloorEffectContract,
)

ROW_RE = re.compile(r"^(?:(\S+?):)?L(\d+) (.*)$")
TICKET_RE = re.compile(r"\bOMN-\d+\b")
PR_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_.-]*#\d+\b")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
ID_KEY_RE = re.compile(
    r"^(?:lane|from|ticket|tickets|pr|prs|id|re|until|task|state|subject|verdict|outcome|closes-claim|closes-CLAIM)$"
)


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
        for cell in cells[2:]:
            key, sep, value = cell.partition("=")
            if sep and ID_KEY_RE.fullmatch(key):
                tokens = value.split()
                fields.setdefault(key, tokens[0] if tokens else "")
            elif sep and key == "reason":
                fields.setdefault(key, value.strip()[:300])
        free_text = cells[-1] if not re.match(r"^[A-Za-z_-]+=", cells[-1]) else ""
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
        )
        if row.kind in {"CLAIM", "TERMINAL", "HOLD", "RELEASE", "STATUS"}:
            kept.append((order, row))
        elif row.lane and (
            row.lane not in latest
            or (ts, order) > (latest[row.lane][1].ts, latest[row.lane][0])
        ):
            latest[row.lane] = (order, row)
    ordered = sorted([*kept, *latest.values()], key=lambda pair: (pair[1].ts, pair[0]))
    return tuple(row for _, row in ordered)


def read_ledger_rows(
    *,
    window_days: int,
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
        query += ["--limit", "1000000"]
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
            prs.append(
                ModelFloorPr(
                    key=key,
                    state=facts["state"],
                    draft=facts.get("draft", False),
                    red=cls == "red"
                    or "red" in cls.split("-")
                    or bool((value.get("ci") or {}).get("red")),
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
                kind = {
                    "in progress": "started",
                    "in review": "started",
                    "done": "completed",
                    "canceled": "canceled",
                    "cancelled": "canceled",
                    "duplicate": "canceled",
                }.get(status.lower(), "")
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
                        status_type=issue.get("statusType") or "",
                    )
                )
        return tuple(sorted(issues, key=lambda issue: issue.id))


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
        if sep and key.strip(" *_").casefold() in wanted:
            lines.append(line)
    return "\n".join(lines)


def load_plans(
    *,
    kb: Path,
    plans_relpath: str,
    manifest_suffix: str,
    header_keys: tuple[str, ...],
    contains: tuple[str, ...],
    plans_today: dict[str, Any] | None,
) -> tuple[ModelFloorPlanInput, ...]:
    directory = kb / plans_relpath
    with _source(directory):
        if not directory.is_dir():
            raise SourceRefusedError(
                f"plans directory {directory} is missing: restore it or correct plans_relpath and kb"
            )
        paths = sorted(directory.rglob("*.md"))
    entries = {entry["path"]: entry for entry in (plans_today or {}).get("plans", [])}
    plans: list[ModelFloorPlanInput] = []
    for path in paths:
        relative = path.relative_to(kb).as_posix()
        if path.name.endswith(".readable.md") or (
            contains and not any(value in relative for value in contains)
        ):
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
            entry = entries.get(relative)
            header = header_text(text, header_keys)
            title = re.search(r"^# (.*)", text, re.MULTILINE)
            plans.append(
                ModelFloorPlanInput(
                    plan_path=relative,
                    title=title.group(1).strip() if title else "",
                    manifest=manifest,
                    tickets=tuple(entry.get("tickets", ()))
                    if entry is not None
                    else tuple(sorted(set(TICKET_RE.findall(header)))),
                    prs=tuple(entry.get("prs", ()))
                    if entry is not None
                    else tuple(sorted(set(PR_RE.findall(header)))),
                    table_tasks=tuple(
                        ModelFloorTableTask.model_validate(task)
                        for task in entry.get("tasks", ())
                    )
                    if entry is not None
                    else (),
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
