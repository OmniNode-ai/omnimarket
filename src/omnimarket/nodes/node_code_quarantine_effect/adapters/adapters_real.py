# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real seams: a state file and catalog file, git in each repository root, gate commands, the ledger append.

Every command is an argument vector run without a shell, in the repository root. A gate command reads
``ONEX_QUARANTINE_REPOSITORY``, ``ONEX_QUARANTINE_ENTITY`` and ``ONEX_QUARANTINE_EXCLUDED`` (the excluded paths,
one per line) and exits 0 when the gate is green.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_code_quarantine_effect.models import (
    EnumEntityState,
    ModelCatalogRow,
    ModelFilesBackend,
    ModelGateCommand,
    ModelQuarantineEvent,
    ModelQuarantineRecord,
    ModelQuarantineReport,
    one_line,
)
from omnimarket.nodes.node_code_quarantine_effect.protocols import QuarantineStepError

EnvRunner = Callable[
    [Sequence[str], int, Path, Mapping[str, str]], "subprocess.CompletedProcess[str]"
]


def run_in(
    argv: Sequence[str], timeout: int, cwd: Path, extra_env: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, **extra_env},
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"timed out after {timeout}s"
        )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:300]
    return "no output"


def _root(spec: ModelFilesBackend, repository: str) -> Path:
    try:
        return Path(spec.repository_roots[repository])
    except KeyError as exc:
        raise QuarantineStepError(
            "repository", f"the overlay names no root for repository {repository!r}"
        ) from exc


class JsonFileStore:
    """The state is one json file of records; the catalog is one json line per row. Both are written whole."""

    def __init__(self, spec: ModelFilesBackend) -> None:
        self._state = Path(spec.state_file)
        self._catalog = Path(spec.catalog_file)

    def _records(self) -> dict[str, ModelQuarantineRecord]:
        if not self._state.exists():
            return {}
        try:
            raw = json.loads(self._state.read_text(encoding="utf-8"))
            return {
                key: ModelQuarantineRecord.model_validate(value)
                for key, value in raw["records"].items()
            }
        except (OSError, ValueError, KeyError) as exc:
            raise QuarantineStepError("store", f"state file unreadable: {exc}") from exc

    def get(self, repository: str, entity: str) -> ModelQuarantineRecord | None:
        return self._records().get(f"{repository}::{entity}")

    def put(self, record: ModelQuarantineRecord) -> None:
        records = self._records()
        records[f"{record.repository}::{record.entity}"] = record
        document = {
            "records": {k: json.loads(v.model_dump_json()) for k, v in records.items()}
        }
        try:
            self._state.parent.mkdir(parents=True, exist_ok=True)
            staged = self._state.with_suffix(".tmp")
            staged.write_text(
                json.dumps(document, indent=1, sort_keys=True), encoding="utf-8"
            )
            staged.replace(self._state)
        except OSError as exc:
            raise QuarantineStepError(
                "store", f"state file not written: {exc}"
            ) from exc

    def excluded_paths(self, repository: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    path
                    for record in self._records().values()
                    if record.repository == repository
                    and record.state is EnumEntityState.QUARANTINED
                    for path in (*record.entity_paths, *record.test_paths)
                }
            )
        )

    def append_catalog(
        self, repository: str, rows: tuple[ModelCatalogRow, ...]
    ) -> None:
        try:
            self._catalog.parent.mkdir(parents=True, exist_ok=True)
            with self._catalog.open("a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(
                        json.dumps(
                            {
                                "repository": repository,
                                **json.loads(row.model_dump_json()),
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
        except OSError as exc:
            raise QuarantineStepError("store", f"catalog not written: {exc}") from exc


class GitRepository:
    def __init__(self, spec: ModelFilesBackend, run: EnvRunner = run_in) -> None:
        self._s = spec
        self._run = run

    def path_exists(self, repository: str, path: str) -> bool:
        return (_root(self._s, repository) / path).exists()

    def tag_sha(self, repository: str, tag: str) -> str:
        done = self._run(
            [
                self._s.git_binary,
                "rev-parse",
                "-q",
                "--verify",
                f"refs/tags/{tag}^{{commit}}",
            ],
            self._s.git_timeout_seconds,
            _root(self._s, repository),
            {},
        )
        return done.stdout.strip() if done.returncode == 0 else ""

    def remove_paths(self, repository: str, paths: tuple[str, ...]) -> None:
        done = self._run(
            [self._s.git_binary, "rm", "-r", "-q", "--", *paths],
            self._s.git_timeout_seconds,
            _root(self._s, repository),
            {},
        )
        if done.returncode:
            raise QuarantineStepError(
                "repository",
                f"git rm exited {done.returncode}: {_first_line(done.stderr)}",
            )


class CommandGates:
    def __init__(self, spec: ModelFilesBackend, run: EnvRunner = run_in) -> None:
        self._s = spec
        self._run = run

    def _green(
        self,
        command: ModelGateCommand,
        repository: str,
        entity: str,
        excluded: tuple[str, ...],
    ) -> bool:
        done = self._run(
            command.argv,
            command.timeout_seconds,
            _root(self._s, repository),
            {
                "ONEX_QUARANTINE_REPOSITORY": repository,
                "ONEX_QUARANTINE_ENTITY": entity,
                "ONEX_QUARANTINE_EXCLUDED": "\n".join(excluded),
            },
        )
        return done.returncode == 0

    def clean_build(self, repository: str, excluded: tuple[str, ...]) -> bool:
        return self._green(self._s.clean_build, repository, "", excluded)

    def lab_runtime_start(self, repository: str, excluded: tuple[str, ...]) -> bool:
        return self._green(self._s.lab_runtime_start, repository, "", excluded)

    def reading_present(self, repository: str, entity: str) -> bool:
        return self._green(self._s.reading, repository, entity, ())


class AppendReporter:
    """One STATUS row per event, appended by the ledger command the overlay names."""

    def __init__(self, spec: ModelQuarantineReport, run: EnvRunner = run_in) -> None:
        self._r = spec
        self._run = run

    def row(self, event: ModelQuarantineEvent, now: datetime) -> str:
        return " | ".join(
            [
                now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "STATUS",
                f"lane={self._r.lane}",
                "src=code-quarantine",
                f"actor={self._r.actor}",
                "model=none",
                f"run={event.correlation_id}",
                f"kind={event.kind}",
                f"outcome={event.outcome.value}",
                f"reading={event.reading}",
                f"repo={event.repository}",
                one_line(event.detail),
            ]
        )

    def report(self, event: ModelQuarantineEvent) -> None:
        done = self._run(
            [*self._r.append_argv, self.row(event, datetime.now(UTC))],
            self._r.timeout_seconds,
            Path.cwd(),
            self._r.environment,
        )
        if done.returncode:
            raise QuarantineStepError(
                "report",
                f"the ledger append exited {done.returncode}: {_first_line(done.stderr)}",
            )
