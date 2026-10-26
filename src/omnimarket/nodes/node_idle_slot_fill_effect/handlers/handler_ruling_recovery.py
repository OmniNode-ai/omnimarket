# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The throughput tick's effect around ruled-work detection and declared live checks."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models.model_ruling_recovery import ModelRulingRecoveryResult
from .ruling_live_checks import Finding, evaluate_check, packaged_checks, read_check
from .ruling_recovery import detect_relaunches, parse_rows


def read_ledger(path: Path) -> list[dict[str, str]]:
    """Read all split rolls: a ruling and its unfinished work never age out."""
    files = [
        *sorted((path.parent / "archive").glob("ROLLING_WORK_LEDGER_*-split.md")),
        path,
    ]
    text = "\n".join(p.read_text(encoding="utf-8") for p in files)
    # Split rolls overlap at their boundary; one ledger row remains one fact.
    return parse_rows("\n".join(dict.fromkeys(text.splitlines())))


def _briefs(rows: list[dict[str, str]], roots: list[Path]) -> dict[str, str]:
    result = {}
    for row in rows:
        if row["action"] != "CLAIM" or not row.get("lane"):
            continue
        lane = row["lane"]
        if "/" in lane or lane in {".", ".."}:
            continue
        paths = [Path(row["brief"])] if row.get("brief") else []
        paths.extend(root / f"{lane}.brief.md" for root in roots)
        for path in paths:
            try:
                result[lane] = path.read_text(encoding="utf-8")
                break
            except (OSError, UnicodeError):
                continue
    return result


def _local_artifacts(rows: list[dict[str, str]], run_root: Path) -> None:
    """Retain actual staged patch locations when the blocked run is on this host.

    Remote run ids and their host remain in the brief when their files cannot be
    read here. A path is filled only from an existing run, never invented.
    """
    for row in rows:
        run = row.get("run", "")
        if row["action"] != "CLAIM" or not run or "/" in run or run in {".", ".."}:
            continue
        directory = run_root / run
        if not directory.is_dir():
            continue
        row.setdefault("run-directory", str(directory))
        context = run_root / f"{run}.ctx"
        try:
            repository = (context / "repository").read_text(encoding="utf-8").strip()
            if repository and all(c.isalnum() or c in "_/.-" for c in repository):
                row.setdefault("repo", repository.split("/")[-1])
        except (OSError, UnicodeError):
            pass
        patches = [
            context / "landing-artifact/change.patch",
            *sorted((context / "scratch").glob("*staged*.patch")),
        ]
        existing = [str(p) for p in patches if p.is_file()]
        if existing:
            row.setdefault("patch-path", ", ".join(existing))


def _record_exceptions(
    path: Path,
    rows: list[dict[str, str]],
    now: str,
    parent: str | None,
    record: Callable[[str, str, str], str | None] | None,
) -> None:
    """One durable exception row per failed retry; a failed ledger append retries next tick."""
    path = path.resolve()
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(saved, dict):
        raise ValueError("exception store must be a mapping")
    for row in rows:
        old = saved.get(row["key"], {})
        if not isinstance(old, dict):
            raise ValueError("exception row must be a mapping")
        entry: dict[str, Any] = {
            "action": "EXCEPTION",
            "at": now,
            **row,
            "owner": parent or "orchestrator",
        }
        if old.get("ledger_recorded"):
            entry = old
        elif record is not None:
            error = record(
                "ruling-relaunch-exception",
                row["ticket"],
                json.dumps(entry, sort_keys=True),
            )
            if error:
                raise OSError(f"exception ledger append refused: {error}")
            entry["ledger_recorded"] = True
        saved[row["key"]] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as output:
        temporary = Path(output.name)
        json.dump(saved, output, sort_keys=True)
        output.write("\n")
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class HandlerRulingRecovery:
    """Read checks on every call, then offer abandoned ruled work to the existing actor."""

    def __init__(
        self, live_reader: Callable[[Mapping[str, Any]], Any] | None = None
    ) -> None:
        self._live_reader = live_reader or read_check

    def handle(
        self,
        *,
        now: str,
        state_dir: Path,
        sent: Mapping[str, Any],
        parent: str | None = None,
        read_only: bool = False,
        record: Callable[[str, str, str], str | None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> ModelRulingRecoveryResult:
        environ = os.environ if env is None else env
        found: list[Finding] = []
        checks = {c["ruling"]: c for c in packaged_checks()}
        rows: list[dict[str, str]] = []
        registry = environ.get("ONEX_REGISTRY_ROOT") or environ.get("OMNI_HOME")
        path = environ.get("ONEX_LEDGER_PATH")
        ledger = (
            Path(path)
            if path
            else Path(registry) / "docs/tracking/ROLLING_WORK_LEDGER.md"
            if registry
            else None
        )
        try:
            if ledger is None:
                raise ValueError("ONEX_LEDGER_PATH or ONEX_REGISTRY_ROOT is required")
            rows = read_ledger(ledger)
        except (OSError, UnicodeError, ValueError) as exc:
            found.append(
                (
                    "unknown",
                    "ruling-relaunch-ledger",
                    f"cannot read ruling history: {type(exc).__name__}: {exc}",
                    "restore the ledger read; unread history never authorizes a relaunch",
                )
            )
        for row in rows:
            if (
                row["action"] != "RULING"
                or row.get("kind") != "process"
                or "live-check" not in row
                or row["at"] > now
            ):
                continue
            try:
                declaration = json.loads(row["live-check"])
                if not isinstance(declaration, dict):
                    raise ValueError("one check must be a JSON object")
                checks[row["at"]] = {**declaration, "ruling": row["at"]}
            except ValueError:
                found.append(
                    (
                        "missing",
                        f"ruling-drift:{row['at']}",
                        "live=unread; live-check declaration is invalid",
                        "declare one supported live-check JSON object on the process ruling",
                    )
                )
        for check in checks.values():
            try:
                reading = self._live_reader(check)
                finding = evaluate_check(check, reading)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                finding = (
                    "missing",
                    f"ruling-drift:{check['ruling']}",
                    f"live=unread ({type(exc).__name__})",
                    "restore the declared live reader",
                )
            if finding:
                found.append(finding)
        roots = [state_dir / "lab-fill-briefs"]
        roots.extend(
            Path(p)
            for p in environ.get("ONEX_LANE_BRIEF_DIRS", "").split(os.pathsep)
            if p
        )
        if registry:
            roots.append(Path(registry) / ".claude_scratch/lanes")
        briefs = _briefs(rows, roots)
        _local_artifacts(
            rows,
            Path(
                environ.get("ONEX_REMOTE_LANE_RUN_ROOT") or Path.home() / "lab-run/runs"
            ),
        )
        items, exceptions = detect_relaunches(
            rows,
            datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC),
            briefs=briefs,
            sent=sent,
        )
        for item in items:
            found.append(
                (
                    "note",
                    f"ruling-relaunch:{item['ticket']}",
                    f"queued {item['key']}; blocker={item['blocker']}",
                    "Codex-first through the lab fill; existing scope, admission and runner gates apply",
                )
            )
            if not item["repo"]:
                found.append(
                    (
                        "unknown",
                        f"ruling-relaunch-scope:{item['ticket']}",
                        "recovery item has no readable owning repository; dispatch held",
                        "restore the original brief or run receipt so the existing repository scope gate can judge it",
                    )
                )
        for row in exceptions:
            found.append(
                (
                    "missing",
                    f"ruling-relaunch-exception:{row['ticket']}",
                    f"second block: lane={row['lane']} blocker={row['blocker']}; owner={parent or 'orchestrator'}",
                    "the orchestrator resolves this exception; no automatic third attempt",
                )
            )
        if exceptions and not read_only:
            try:
                _record_exceptions(
                    state_dir / "ruling-relaunch.exceptions.json",
                    exceptions,
                    now,
                    parent,
                    record,
                )
            except (OSError, ValueError) as exc:
                found.append(
                    (
                        "unknown",
                        "ruling-relaunch-exceptions",
                        f"cannot record exception rows: {exc}",
                        "restore the exception queue; the next tick retries and continues to print the block",
                    )
                )
        return ModelRulingRecoveryResult(items, found)
