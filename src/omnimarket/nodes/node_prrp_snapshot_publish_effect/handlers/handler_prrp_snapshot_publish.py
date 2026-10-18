# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the gate's Actions-variables snapshot per registry repository; put it on every lab host.

The private-repo-runner-placement gate refuses a lab host's workflow edit when it cannot read
repository visibility or the organisation and repository Actions variables, and a lab host cannot
(HTTP 403; its GitHub reads are refused). The gate accepts a dated snapshot instead
(``PRRP_VARIABLES_SNAPSHOT``), written by ``--write-variables-snapshot`` from a host whose
credential can read them. That flag writes ONE repository's facts, and a lab host reads ONE file, so
this handler reads each registry repository live, merges the answers into one file dated by its
oldest read, and copies the file to each available host supplied by the caller.

Complete or nothing: a repository the gate could not read fails the run before any file is written,
so a host never holds a partial snapshot (the gate would refuse it anyway as silent about a
repository). A host that could not be reached, or whose copy did not read back byte-identical, fails
the run, but the other hosts still get the file. The heartbeat is written only when every available
host holds the file, so its age is the age of the last complete run; every failure writes
``failure.json`` and is returned for the CLI to print as an ALARM.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from ..models import (
    ModelPrrpSnapshotHost,
    ModelPrrpSnapshotHostOutcome,
    ModelPrrpSnapshotPublishRequest,
    ModelPrrpSnapshotPublishResult,
)

SNAPSHOT_SCHEMA = 1
HEARTBEAT = "heartbeat.json"
FAILURE = "failure.json"
_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_SSH_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
)


def _registry_slugs(repos_file: Path) -> list[str]:
    document = yaml.safe_load(repos_file.read_text(encoding="utf-8")) or {}
    slugs: list[str] = []
    for row in document.get("repositories") or []:
        slug = row.get("repo") if isinstance(row, dict) else None
        if isinstance(slug, str) and slug not in slugs:
            slugs.append(slug)
    if not slugs:
        raise ValueError(f"{repos_file} lists no repositories")
    return slugs


def _read_one(
    request: ModelPrrpSnapshotPublishRequest, slug: str, scratch: Path, index: int
) -> dict[str, Any]:
    target = scratch / f"{index}.json"
    try:
        done = subprocess.run(
            [
                str(request.python),
                str(request.gate_script),
                "--repo",
                slug,
                "--write-variables-snapshot",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=request.gate_timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"{slug}: the gate did not run ({error})") from error
    if done.returncode:
        raise ValueError(
            f"{slug}: the gate exited {done.returncode}: {done.stderr.strip()[:300]}"
        )
    try:
        body = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            f"{slug}: the gate wrote no readable snapshot ({error})"
        ) from error
    stamp = body.get("captured_at") if isinstance(body, dict) else None
    if (
        body.get("schema") != SNAPSHOT_SCHEMA
        or not isinstance(stamp, str)
        or not _STAMP.match(stamp)
        or not isinstance(body.get("visibility"), dict)
        or not isinstance(body.get("scopes"), dict)
    ):
        raise ValueError(f"{slug}: the gate wrote a snapshot of an unknown shape")
    if slug not in body["visibility"]:
        raise ValueError(f"{slug}: the snapshot does not record its visibility")
    return dict(body)


def merge_snapshots(pieces: list[dict[str, Any]]) -> dict[str, Any]:
    """One snapshot holding every piece's facts, dated by the oldest read."""
    visibility: dict[str, str] = {}
    scopes: dict[str, dict[str, str]] = {}
    for piece in pieces:
        visibility.update(piece["visibility"])
        scopes.update(piece["scopes"])
    return {
        "schema": SNAPSHOT_SCHEMA,
        "captured_at": min(piece["captured_at"] for piece in pieces),
        "visibility": visibility,
        "scopes": scopes,
    }


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    scratch.write_bytes(data)
    scratch.replace(path)


_REMOTE_WRITE = (
    'set -eu; dest="$HOME"/{rel}; dir=$(dirname "$dest"); mkdir -p "$dir"; '
    'tmp="$dir/.$(basename "$dest").$$.tmp"; trap \'rm -f "$tmp"\' EXIT; '
    'cat > "$tmp"; mv -f "$tmp" "$dest"'
)
_REMOTE_READ = 'cat "$HOME"/{rel}'


def _send(
    request: ModelPrrpSnapshotPublishRequest, host: ModelPrrpSnapshotHost, data: bytes
) -> str:
    """Copy the file to one remote host over the lab's ssh transport and read it back."""
    rel = shlex.quote(request.destination)
    for body, stdin in (
        (_REMOTE_WRITE.format(rel=rel), data),
        (_REMOTE_READ.format(rel=rel), None),
    ):
        try:
            done = subprocess.run(
                [request.ssh_program, *_SSH_OPTIONS, host.target, body],
                input=stdin,
                capture_output=True,
                timeout=request.ssh_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return f"ssh to {host.target} did not complete ({error})"
        if done.returncode:
            return (
                f"ssh to {host.target} exited {done.returncode}: "
                f"{done.stderr.decode(errors='replace').strip()[:300]}"
            )
        if stdin is None and done.stdout != data:
            return f"the copy on {host.target} did not read back identical to what was sent"
    return ""


class HandlerPrrpSnapshotPublish:
    def handle(
        self, request: ModelPrrpSnapshotPublishRequest
    ) -> ModelPrrpSnapshotPublishResult:
        failures: list[str] = []
        outcomes: list[ModelPrrpSnapshotHostOutcome] = []
        captured_at: str | None = None
        repos = 0
        try:
            slugs = _registry_slugs(request.repos_file)
        except (OSError, ValueError, yaml.YAMLError) as error:
            failures.append(str(error))
            slugs = []
        with tempfile.TemporaryDirectory(prefix="prrp-snapshot-") as scratch_name:
            pieces: list[dict[str, Any]] = []
            for index, slug in enumerate(slugs):
                try:
                    pieces.append(_read_one(request, slug, Path(scratch_name), index))
                except ValueError as error:
                    failures.append(str(error))
            if pieces and not failures:
                merged = merge_snapshots(pieces)
                captured_at, repos = merged["captured_at"], len(slugs)
                data = (json.dumps(merged, indent=2, sort_keys=True) + "\n").encode()
                for host in request.hosts:
                    if not host.available:
                        outcomes.append(
                            ModelPrrpSnapshotHostOutcome(
                                host=host.name,
                                status="skipped",
                                detail=host.unavailable_reason,
                            )
                        )
                        continue
                    if host.local:
                        try:
                            _write_atomic(request.home / request.destination, data)
                            problem = ""
                        except OSError as error:
                            problem = f"local write failed ({error})"
                    else:
                        problem = _send(request, host, data)
                    if problem:
                        failures.append(f"{host.name}: {problem}")
                    outcomes.append(
                        ModelPrrpSnapshotHostOutcome(
                            host=host.name,
                            status="failed" if problem else "written",
                            detail=problem,
                        )
                    )
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        request.state_dir.mkdir(parents=True, exist_ok=True)
        if failures:
            _write_atomic(
                request.state_dir / FAILURE,
                (
                    json.dumps({"at": now, "failures": failures}, indent=2) + "\n"
                ).encode(),
            )
        else:
            _write_atomic(
                request.state_dir / HEARTBEAT,
                (
                    json.dumps(
                        {
                            "at": now,
                            "captured_at": captured_at,
                            "repos": repos,
                            "hosts": {o.host: o.status for o in outcomes},
                        },
                        indent=2,
                    )
                    + "\n"
                ).encode(),
            )
            (request.state_dir / FAILURE).unlink(missing_ok=True)
        return ModelPrrpSnapshotPublishResult(
            exit_code=1 if failures else 0,
            captured_at=captured_at,
            repos=repos,
            hosts=tuple(outcomes),
            failures=tuple(failures),
        )
