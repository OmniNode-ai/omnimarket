# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The red-facts record store and digest of node_job_log_filler_effect (OMN-23832).

``build_record`` turns the end of a red job's log into one typed record: the failing step and a
capped digest of the failing lines. A log that is pytest's junit XML goes through
node_pytest_failure_digest_compute; any other log takes a capped tail of the lines that mark a
failure. ``RedFactsStore`` keeps one file per repository, pull request, head and job, and
``answer_red_facts`` reads only the asked head's directory, so a record of an older head is never
returned for a newer one.

This module imports neither the filler's handler nor its readers: ``onex scm red-facts`` imports
it, and the filler's readers import the scm package.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.scm import ModelScmLocalReadRequest, ModelScmLocalReadResult
from omnimarket.nodes.node_job_log_filler_effect.models.model_job_log_filler import (
    OVERLAY_FILE_ENV,
    OVERLAY_ID,
    ModelJobLogFillerOverlay,
)
from omnimarket.nodes.node_job_log_filler_effect.models.model_red_facts import (
    EnumRedFactsStatus,
    ModelRedFactsAnswer,
    ModelRedFactsRecord,
)
from omnimarket.nodes.node_pytest_failure_digest_compute import (
    ModelPytestRunReport,
    digest_pytest_run,
)

_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_HEAD = re.compile(r"^[0-9a-f]{7,40}$")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z ?")
_SECRET = re.compile(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+|Bearer\s+\S+")
_MARKERS = (
    "##[error]",
    "FAILED ",
    "ERROR ",
    "Error:",
    "AssertionError",
    "Traceback",
)
_GROUP = "##[group]"
_ERROR = "##[error]"


def load_red_facts_overlay(path: Path | None = None) -> ModelJobLogFillerOverlay:
    """The filler's overlay: the explicit file, else the one ONEX_SKILL_OVERLAY_ROOTS holds."""
    explicit = yaml.safe_load(path.read_text(encoding="utf-8")) if path else None
    return load_structured_overlay(
        OVERLAY_ID,
        ModelJobLogFillerOverlay,
        explicit=explicit,
        file_env=OVERLAY_FILE_ENV,
    )


def _clean_lines(text: str) -> list[str]:
    cleaned = (
        _SECRET.sub("redacted", _STAMP.sub("", _ANSI.sub("", raw))).rstrip()
        for raw in text.splitlines()
    )
    return list(cleaned)


def _is_failure_line(line: str) -> bool:
    return line.startswith("E ") or any(marker in line for marker in _MARKERS)


def build_record(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    job_name: str,
    job_id: int,
    text: str,
    total_bytes: int,
    cache_path: str | None,
    overlay: ModelJobLogFillerOverlay,
    now: datetime,
) -> ModelRedFactsRecord:
    """One record from the end of a red job's log, every bound from the overlay."""
    lines = _clean_lines(text)
    stripped = text.lstrip()
    failing_step: str | None = None
    if stripped.startswith(("<?xml", "<testsuite")):
        digest = digest_pytest_run(ModelPytestRunReport(junit_xml=text, exit_code=1))
        kept = [
            line
            for line in (
                digest.failing_node_id,
                digest.exception_type,
                digest.message,
                digest.top_frame,
            )
            if line
        ]
        kind = "junit"
    else:
        first_error = next(
            (i for i, line in enumerate(lines) if _ERROR in line), len(lines)
        )
        for line in reversed(lines[:first_error]):
            if _GROUP in line:
                failing_step = line.split(_GROUP, 1)[1].strip() or None
                break
        kept = [line for line in lines if _is_failure_line(line)]
        if not kept:
            kept = [line for line in lines if line.strip()]
        kind = "log_lines"
    digest_lines = tuple(
        line[: overlay.red_facts_line_chars]
        for line in kept[-overlay.red_facts_max_lines :]
    )
    return ModelRedFactsRecord(
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        check=job_name,
        job_id=job_id,
        failing_step=failing_step,
        digest_lines=digest_lines,
        digest_kind=kind,
        cache_path=cache_path,
        log_total_bytes=total_bytes,
        recorded_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


class RedFactsStore:
    """One JSON file per repository, pull request, head and job under a root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @classmethod
    def from_overlay(
        cls,
        overlay: ModelJobLogFillerOverlay,
        environ: Mapping[str, str] | None = None,
    ) -> RedFactsStore:
        env = os.environ if environ is None else environ
        state = env.get(overlay.red_facts_state_env, "").strip()
        if not state:
            raise ValueError(
                f"the overlay names {overlay.red_facts_state_env} for the state directory, "
                "and it is unset on this host"
            )
        return cls(Path(state) / overlay.red_facts_subdir)

    def _head_dir(self, repo: str, pr_number: int, head_sha: str) -> Path:
        if _REPO.match(repo) is None or _HEAD.match(head_sha) is None:
            raise ValueError(f"unsafe red-facts key {repo!r} {head_sha!r}")
        owner, name = repo.split("/", 1)
        return self._root / owner / name / str(pr_number) / head_sha

    def has(self, repo: str, pr_number: int, head_sha: str, job_id: int) -> bool:
        return (self._head_dir(repo, pr_number, head_sha) / f"{job_id}.json").is_file()

    def write(self, record: ModelRedFactsRecord) -> Path:
        directory = self._head_dir(record.repo, record.pr_number, record.head_sha)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{record.job_id}.json"
        temporary = directory / f".{record.job_id}.json.tmp"
        temporary.write_text(record.model_dump_json(), encoding="utf-8")
        os.replace(temporary, target)
        return target

    def read(
        self, repo: str, pr_number: int, head_sha: str
    ) -> tuple[ModelRedFactsRecord, ...]:
        directory = self._head_dir(repo, pr_number, head_sha)
        if not directory.is_dir():
            return ()
        records: list[ModelRedFactsRecord] = []
        for path in directory.glob("*.json"):
            try:
                records.append(
                    ModelRedFactsRecord.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValidationError):
                continue
        return tuple(sorted(records, key=lambda record: record.job_id))


def answer_red_facts(
    *,
    store: RedFactsStore,
    repo: str,
    pr_number: int,
    head_sha: str,
    head_age_seconds: int | None = None,
) -> ModelRedFactsAnswer:
    """The records of the asked head, else ``not_filled``: never another head's text."""
    records = store.read(repo, pr_number, head_sha)
    if records:
        return ModelRedFactsAnswer(
            status=EnumRedFactsStatus.FOUND,
            repo=repo,
            pr_number=pr_number,
            head_sha=head_sha,
            records=records,
            head_age_seconds=head_age_seconds,
        )
    return ModelRedFactsAnswer(
        status=EnumRedFactsStatus.NOT_FILLED,
        repo=repo,
        pr_number=pr_number,
        head_sha=head_sha,
        reason=(
            f"no red-facts record for head {head_sha[:10]}; "
            "the filler has not filled this head yet"
        ),
        head_age_seconds=head_age_seconds,
    )


def answer_red_facts_read(
    request: ModelScmLocalReadRequest, checks: ModelScmLocalReadResult
) -> ModelScmLocalReadResult:
    """The already-read PR_CHECKS head, answered from the red-facts store."""
    if not checks.answered:
        return checks.model_copy(update={"read": request.read})
    head = checks.result.get("head_sha")
    if not isinstance(head, str) or not head or request.pr_number is None:
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=False,
            unanswered_reason="the watcher record has no head commit",
        )
    raw = checks.result.get("read_at")
    read_at: datetime | None = None
    if isinstance(raw, str) and raw:
        try:
            read_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            read_at = None
        else:
            read_at = read_at if read_at.tzinfo else read_at.replace(tzinfo=UTC)
    head_age_seconds = (
        max(0, int((datetime.now(UTC) - read_at).total_seconds()))
        if read_at is not None
        else None
    )
    try:
        store = RedFactsStore.from_overlay(load_red_facts_overlay())
        answer = answer_red_facts(
            store=store,
            repo=request.repo,
            pr_number=request.pr_number,
            head_sha=head,
            head_age_seconds=head_age_seconds,
        )
    except (NodeOverlayError, ValueError, OSError) as exc:
        return ModelScmLocalReadResult(
            read=request.read,
            repo=request.repo,
            pr_number=request.pr_number,
            answered=False,
            unanswered_reason=f"the red-facts store is unreadable: {exc}",
        )
    return ModelScmLocalReadResult(
        read=request.read,
        repo=request.repo,
        pr_number=request.pr_number,
        answered=True,
        source="watcher",
        watcher_tick=checks.watcher_tick,
        result=answer.model_dump(mode="json"),
    )


__all__ = [
    "RedFactsStore",
    "answer_red_facts",
    "answer_red_facts_read",
    "build_record",
    "load_red_facts_overlay",
]
