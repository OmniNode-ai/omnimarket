# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-declared repository doctrine context source (OMN-21090)."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_span_unit import EnumContextSpanUnit
from omnibase_core.models.context.model_context_source_entry import (
    ModelContextSourceEntry,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)
from omnibase_core.models.context.model_context_source_span import (
    ModelContextSourceSpan,
)
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_context_pack_builder_compute.parsers.parser_guidance_section import (
    GuidanceSectionParser,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RULING = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| RULING \| ")


def _revision_stamp(value: str) -> datetime | None:
    if _STAMP.fullmatch(value):
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _entry(
    factor: EnumContextFactor,
    text: str,
    source_ref: str,
    as_of: datetime,
    *,
    paraphrase: bool = False,
) -> ModelContextSourceEntry:
    return ModelContextSourceEntry(
        factor=factor,
        text=text,
        source_ref=source_ref,
        span=ModelContextSourceSpan(
            unit=EnumContextSpanUnit.CHARACTER, start=0, end=len(text)
        ),
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        status=EnumContextSourceStatus.RESOLVED,
        verbatim_verified=not paraphrase,
        paraphrase=paraphrase,
        as_of=as_of,
    )


class ModelRepoDoctrineDocument(BaseModel):
    """One repository guidance document and its selected headings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor
    path: str = Field(min_length=1)
    headings: tuple[str, ...] = Field(min_length=1)


class ModelRepoDoctrineExemplar(BaseModel):
    """Git references, caps and conventional types for each lane kind."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str = Field(min_length=1)
    fallback_ref: str = Field(min_length=1)
    scan_limit: int = Field(gt=0)
    max_files: int = Field(gt=0)
    lane_kind_commit_types: dict[str, tuple[str, ...]]


class ModelRepoDoctrineBinding(BaseModel):
    """The contract's context_source_repo_doctrine block."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root_env: str = Field(min_length=1)
    repository_path: str = Field(min_length=1)
    documents: tuple[ModelRepoDoctrineDocument, ...] = Field(min_length=1)
    registry_glob: str = Field(min_length=1)
    ledger_env: str = Field(min_length=1)
    ledger_archive_glob: str = Field(min_length=1)
    max_rulings: int = Field(gt=0)
    exemplar: ModelRepoDoctrineExemplar


def load_repo_doctrine_binding(contract_path: Path) -> ModelRepoDoctrineBinding:
    """Load and validate the contract's repository doctrine binding."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    block = raw.get("context_source_repo_doctrine") if isinstance(raw, dict) else None
    if not isinstance(block, dict):
        raise ValueError("context_source_repo_doctrine must be a mapping")
    return ModelRepoDoctrineBinding.model_validate(block)


class HandlerContextSourceRepoDoctrine:
    """Read repository doctrine for the contract-bound context tiers."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        binding: ModelRepoDoctrineBinding | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        self._binding = binding or load_repo_doctrine_binding(self._contract_path)
        self._env = env

    def handle(self, request: ModelContextSourceRequest) -> ModelContextSourceResult:
        if not isinstance(request, ModelContextSourceRequest):
            raise TypeError(
                "repository doctrine context source requires its contract payload"
            )
        if request.factor not in (
            EnumContextFactor.INVARIANTS,
            EnumContextFactor.CANONICAL_SHAPES,
            EnumContextFactor.EXEMPLAR,
        ):
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNRESOLVED)
        env = os.environ if self._env is None else self._env
        value = env.get(self._binding.root_env, "")
        if not value or not Path(value).is_dir():
            return ModelContextSourceResult(status=EnumContextSourceStatus.UNAVAILABLE)
        root = Path(value).resolve()
        repository = request.caller_scope.repository
        if not repository or not _REPOSITORY_NAME.fullmatch(repository):
            return ModelContextSourceResult(
                status=EnumContextSourceStatus.UNRESOLVED,
                entry_counts={"repository_skipped": 1},
            )
        stamp = _revision_stamp(request.revision)
        entries: list[ModelContextSourceEntry] = []
        counts: dict[str, int] = {}
        if request.factor is EnumContextFactor.INVARIANTS:
            entries.extend(
                self._sections(root, repository, request.factor, stamp, counts)
            )
            entries.extend(self._registry(root, repository, stamp, counts))
            entries.extend(self._rulings(repository, env, stamp, counts))
        elif request.factor is EnumContextFactor.CANONICAL_SHAPES:
            entries.extend(
                self._sections(root, repository, request.factor, stamp, counts)
            )
        else:
            return self._exemplar(root, repository, request, stamp)
        return ModelContextSourceResult(
            status=EnumContextSourceStatus.RESOLVED
            if entries
            else EnumContextSourceStatus.UNRESOLVED,
            entries=tuple(entries),
            entry_counts=counts,
        )

    @staticmethod
    def _path(root: Path, template: str, repository: str) -> Path | None:
        target = (root / template.replace("{repository}", repository)).resolve()
        return target if root in target.parents else None

    def _sections(
        self,
        root: Path,
        repository: str,
        factor: EnumContextFactor,
        stamp: datetime | None,
        counts: dict[str, int],
    ) -> list[ModelContextSourceEntry]:
        entries: list[ModelContextSourceEntry] = []
        counts.update(sections=0, documents_skipped=0)
        for document in self._binding.documents:
            if document.factor is not factor:
                continue
            path = self._path(root, document.path, repository)
            if path is None or not path.is_file():
                counts["documents_skipped"] += 1
                continue
            try:
                text = path.read_bytes().decode("utf-8")
                as_of = stamp or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except (OSError, UnicodeError):
                counts["documents_skipped"] += 1
                continue
            relative = path.relative_to(root).as_posix()
            for section in GuidanceSectionParser().parse(text, source_file=relative):
                if (
                    section.heading_path
                    and section.content.strip()
                    and section.heading_path[-1].startswith(document.headings)
                ):
                    entries.append(
                        _entry(
                            factor,
                            section.content,
                            f"{relative}#{section.heading_path[-1]}",
                            as_of,
                        )
                    )
        counts["sections"] = len(entries)
        return entries

    def _registry(
        self,
        root: Path,
        repository: str,
        stamp: datetime | None,
        counts: dict[str, int],
    ) -> list[ModelContextSourceEntry]:
        entries: list[ModelContextSourceEntry] = []
        counts.update(registry=0, registry_skipped=0)
        pattern = self._path(root, self._binding.registry_glob, repository)
        if pattern is None:
            return entries
        for candidate in sorted(
            root.glob(pattern.relative_to(root).as_posix()), key=lambda p: p.name
        ):
            path = candidate.resolve()
            if root not in path.parents or not path.is_file():
                counts["registry_skipped"] += 1
                continue
            try:
                raw = yaml.safe_load(path.read_bytes())
                as_of = stamp or datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except (OSError, UnicodeError, yaml.YAMLError):
                counts["registry_skipped"] += 1
                continue
            if (
                not isinstance(raw, dict)
                or not isinstance(raw.get("principle_code"), str)
                or not isinstance(raw.get("description"), str)
                or not raw["principle_code"].strip()
                or not raw["description"].strip()
            ):
                counts["registry_skipped"] += 1
                continue
            code = raw["principle_code"]
            description = " ".join(raw["description"].split())
            text = f"{code} {raw.get('name', '')} ({raw.get('severity', '')}): {description}"
            entries.append(
                _entry(
                    EnumContextFactor.INVARIANTS,
                    text,
                    f"{path.relative_to(root).as_posix()}#{code}",
                    as_of,
                    paraphrase=True,
                )
            )
        counts["registry"] = len(entries)
        return entries

    def _rulings(
        self,
        repository: str,
        env: Mapping[str, str],
        stamp: datetime | None,
        counts: dict[str, int],
    ) -> list[ModelContextSourceEntry]:
        counts.update(rulings=0, rulings_unavailable=0)
        value = env.get(self._binding.ledger_env, "")
        ledger = Path(value)
        if not value or not ledger.is_absolute() or not ledger.is_file():
            counts["rulings_unavailable"] = 1
            return []
        mentions = re.compile(
            r"(?<![A-Za-z0-9_-])" + re.escape(repository) + r"(?![A-Za-z0-9_])"
        )
        rows: list[tuple[datetime, int, str, str]] = []
        # Archive splits overlap, so one row can be read from two files.
        seen: set[str] = set()

        def collect(path: Path) -> None:
            try:
                lines = path.read_bytes().decode("utf-8").splitlines()
            except (OSError, UnicodeError):
                return
            for line in lines:
                match = _RULING.match(line)
                if match is None or line in seen or not mentions.search(line):
                    continue
                ts = match["ts"]
                when = _revision_stamp(ts)
                if when is None or (stamp is not None and when > stamp):
                    continue
                seen.add(line)
                rows.append((when, len(rows), ts, line))

        collect(ledger)
        if len(rows) < self._binding.max_rulings:
            parent = ledger.resolve().parent
            pattern = self._path(parent, self._binding.ledger_archive_glob, repository)
            if pattern is not None:
                for path in sorted(
                    parent.glob(pattern.relative_to(parent).as_posix()),
                    key=lambda p: p.name,
                    reverse=True,
                ):
                    resolved = path.resolve()
                    if parent in resolved.parents and resolved.is_file():
                        collect(resolved)
                    if len(rows) >= self._binding.max_rulings:
                        break
        selected = sorted(rows, key=lambda row: (row[0], row[1]), reverse=True)[
            : self._binding.max_rulings
        ]
        counts["rulings"] = len(selected)
        return [
            _entry(
                EnumContextFactor.INVARIANTS,
                text,
                # Rows can share a stamp; the row digest keeps each reference unique.
                f"ledger#{ts}@{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                when,
            )
            for when, _, ts, text in selected
        ]

    @staticmethod
    def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            env=scrub_git_location_env(os.environ),
            timeout=10,
            check=False,
            capture_output=True,
            text=True,
        )

    def _exemplar(
        self,
        root: Path,
        repository: str,
        request: ModelContextSourceRequest,
        stamp: datetime | None,
    ) -> ModelContextSourceResult:
        counts = {"exemplar": 0}
        unresolved = ModelContextSourceResult(
            status=EnumContextSourceStatus.UNRESOLVED, entry_counts=counts
        )
        config = self._binding.exemplar
        lane_kind = request.caller_scope.lane_kind
        types = config.lane_kind_commit_types.get(lane_kind) if lane_kind else None
        if not types:
            return unresolved
        repo = self._path(root, self._binding.repository_path, repository)
        if repo is None:
            return unresolved
        if not repo.is_dir():
            return ModelContextSourceResult(
                status=EnumContextSourceStatus.UNAVAILABLE, entry_counts=counts
            )
        try:
            ref = config.ref
            log_args = [
                "--first-parent",
                "-n",
                str(config.scan_limit),
                "--format=%H%x1f%cI%x1f%s",
            ]
            if stamp is not None:
                log_args.append(f"--before={request.revision}")
            log = self._git(repo, "log", ref, *log_args)
            if log.returncode:
                ref = config.fallback_ref
                log = self._git(repo, "log", ref, *log_args)
            if log.returncode:
                return ModelContextSourceResult(
                    status=EnumContextSourceStatus.UNAVAILABLE, entry_counts=counts
                )
            for line in log.stdout.splitlines():
                parts = line.split("\x1f", 2)
                if len(parts) != 3:
                    continue
                sha, commit_time, subject = parts
                pr = re.search(r"\(#(?P<number>\d+)\)$", subject)
                commit_type = re.split(r"[(:]", subject, maxsplit=1)[0].removesuffix(
                    "!"
                )
                if pr is None or commit_type not in types:
                    continue
                # Newer git prints a UTC %cI as "Z", older git as "+00:00";
                # render it from the parsed value so the text is git-independent.
                committed = datetime.fromisoformat(commit_time)
                as_of = stamp or committed
                shown = self._git(repo, "show", "--format=", "--name-status", sha)
                if shown.returncode:
                    return ModelContextSourceResult(
                        status=EnumContextSourceStatus.UNAVAILABLE, entry_counts=counts
                    )
                files = [row for row in shown.stdout.splitlines() if row]
                rendered = files[: config.max_files]
                if len(files) > config.max_files:
                    rendered.append(f"... and {len(files) - config.max_files} more")
                text = (
                    f"{subject}\nmerged {committed.isoformat()} as {sha[:12]} on {ref}\nfiles ({len(files)}):\n"
                    + "\n".join(rendered)
                )
                entry = _entry(
                    EnumContextFactor.EXEMPLAR,
                    text,
                    f"{repository}#{pr['number']}@{sha[:12]}",
                    as_of,
                    paraphrase=True,
                )
                return ModelContextSourceResult(
                    status=EnumContextSourceStatus.RESOLVED,
                    entries=(entry,),
                    entry_counts={"exemplar": 1},
                )
        except (OSError, subprocess.TimeoutExpired):
            return ModelContextSourceResult(
                status=EnumContextSourceStatus.UNAVAILABLE, entry_counts=counts
            )
        return unresolved


__all__ = [
    "HandlerContextSourceRepoDoctrine",
    "ModelRepoDoctrineBinding",
    "ModelRepoDoctrineDocument",
    "ModelRepoDoctrineExemplar",
    "load_repo_doctrine_binding",
]
