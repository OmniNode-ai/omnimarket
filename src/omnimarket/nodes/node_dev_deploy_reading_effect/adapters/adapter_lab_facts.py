# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab's fact sources for piece 11 (OMN-22958): merges from the clone, receipts from the lane-health
projection, the probe from the runtime's health answer and its recorded build commit.

Each source returns what it read and, when it could not read, the reason: a source that cannot be read is
never an empty list. The receipt source reads the projection's one row per lane, which holds the lane's newest
receipt only; an older merge's receipt is not on that row.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from omnimarket.nodes.node_dev_deploy_reading_compute.models.model_dev_deploy_reading import (
    ModelLabPassReceiptFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
    ModelMergeSettings,
    ModelProbeSettings,
    ModelReceiptSettings,
)

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]
Fetch = Callable[[str, int], str]
_START = "\x01"


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of these sources; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


def http_get(url: str, timeout: int) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        body: bytes = response.read()
    return body.decode("utf-8")


def glob_regex(glob: str) -> re.Pattern[str]:
    """``**`` crosses directories, ``*`` and ``?`` stay inside one."""
    out = []
    index = 0
    while index < len(glob):
        if glob.startswith("**", index):
            out.append(".*")
            index += 2
        elif glob[index] == "*":
            out.append("[^/]*")
            index += 1
        elif glob[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(glob[index]))
            index += 1
    return re.compile("".join(out))


def _walk(document: Any, path: Sequence[str]) -> Any:
    node = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    return node


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


class GitRuntimeMergeSource:
    """First-parent commits of the ref inside the lookback, each marked runtime-affecting by its changed files."""

    def __init__(
        self,
        settings: ModelMergeSettings,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._settings = settings
        self._environ = environ
        self._run = run
        self._patterns = tuple(glob_regex(g) for g in settings.runtime_path_globs)

    def read(self, now: datetime) -> tuple[tuple[ModelRuntimeMerge, ...], str | None]:
        settings = self._settings
        clone = self._environ.get(settings.clone_path_env, "").strip()
        if not clone:
            return (), f"{settings.clone_path_env} is not set"
        since = (now - timedelta(seconds=settings.lookback_seconds)).isoformat()
        try:
            done = self._run(
                [
                    "git",
                    "-C",
                    str(Path(clone)),
                    "log",
                    "--first-parent",
                    "-m",
                    "--name-only",
                    f"--since={since}",
                    f"--format={_START}%H%x09%cI",
                    settings.ref,
                    "--",
                ],
                None,
                settings.git_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return (), f"git log failed: {type(exc).__name__}"
        if done.returncode:
            return (), f"git log exited {done.returncode}"
        merges: list[ModelRuntimeMerge] = []
        try:
            for chunk in done.stdout.decode().split(_START)[1:]:
                head, *files = chunk.split("\n")
                sha, stamp = head.split("\t")
                merges.append(
                    ModelRuntimeMerge(
                        sha=sha,
                        merged_at=_utc(stamp),
                        runtime_affecting=any(
                            p.fullmatch(f) for f in files if f for p in self._patterns
                        ),
                    )
                )
        except (ValueError, ValidationError) as exc:
            return (), f"git log output unreadable: {type(exc).__name__}"
        return tuple(merges), None


class LaneHealthReceiptSource:
    """The lane's newest lab pass receipt from the lab runtime's lane-health projection."""

    def __init__(
        self, settings: ModelReceiptSettings, lane: str, fetch: Fetch = http_get
    ) -> None:
        self._settings = settings
        self._lane = lane
        self._fetch = fetch

    def read(self) -> tuple[tuple[ModelLabPassReceiptFact, ...], str | None]:
        try:
            rows = json.loads(
                self._fetch(
                    self._settings.projection_url, self._settings.timeout_seconds
                )
            )["rows"]
            found: list[ModelLabPassReceiptFact] = []
            for row in rows:
                if row.get("lane") != self._lane or not row.get("receipt_sha"):
                    continue
                found.append(
                    ModelLabPassReceiptFact(
                        sha=row["receipt_sha"],
                        lane=self._lane,
                        result=row["receipt_result"],
                        finished_at=_utc(row["receipt_observed_at"]),
                    )
                )
        except ValidationError:
            return (
                (),
                "lane-health receipt row is not a PASS or FAIL receipt for an exact sha",
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return (), f"lane-health projection unreadable: {type(exc).__name__}"
        return tuple(found), None


class RuntimeProbeSource:
    """One functional probe: the runtime answers its health and names the commit its build was made from."""

    def __init__(
        self,
        settings: ModelProbeSettings,
        environ: Mapping[str, str],
        fetch: Fetch = http_get,
        run: Runner = run_process,
    ) -> None:
        self._settings = settings
        self._environ = environ
        self._fetch = fetch
        self._run = run

    def _provenance(self) -> dict[str, Any]:
        s = self._settings
        host = self._environ.get(s.ssh_host_env, "").strip()
        if not host:
            raise ValueError(f"{s.ssh_host_env} is not set")
        script = "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={s.compose_project} --filter "
                f"label=com.docker.compose.service={s.provenance_service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" cat {s.provenance_path}',
            ]
        )
        done = self._run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=10",
                "--",
                host,
                "bash -s",
            ],
            script.encode(),
            s.timeout_seconds,
        )
        if done.returncode:
            raise ValueError(f"the runtime host read exited {done.returncode}")
        document = json.loads(done.stdout)
        if not isinstance(document, dict):
            raise ValueError("the provenance file is not an object")
        return document

    def read(
        self, now: datetime, expected_sha: str
    ) -> tuple[tuple[ModelRuntimeProbeFact, ...], str | None]:
        s = self._settings
        try:
            healthy = _walk(
                json.loads(self._fetch(s.health_url, s.timeout_seconds)),
                s.health_ok_path,
            )
            observed = _walk(self._provenance(), s.sha_path)
            if not isinstance(healthy, bool):
                raise ValueError("the health answer's serving flag is not a boolean")
            fact = ModelRuntimeProbeFact(
                probe_id=f"probe-{now.strftime('%Y%m%dT%H%M%SZ')}",
                probed_at=now,
                expected_sha=expected_sha,
                observed_sha=observed if isinstance(observed, str) else None,
                ok=healthy,
            )
        except ValidationError:
            return (
                (),
                "runtime probe unreadable: the recorded build commit is not an exact sha",
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            subprocess.SubprocessError,
        ) as exc:
            return (), f"runtime probe unreadable: {type(exc).__name__}"
        return (fact,), None


class LabDevDeployFacts:
    """All three sources together. The probe expects the newest runtime-affecting merge past its deadline."""

    def __init__(
        self,
        overlay: ModelDevDeployReadingOverlay,
        environ: Mapping[str, str],
        *,
        run: Runner = run_process,
        fetch: Fetch = http_get,
    ) -> None:
        self._deadline = timedelta(seconds=overlay.thresholds.receipt_deadline_seconds)
        self._merges = GitRuntimeMergeSource(overlay.merges, environ, run)
        self._receipts = LaneHealthReceiptSource(
            overlay.receipts, overlay.thresholds.required_lane, fetch
        )
        self._probe = RuntimeProbeSource(overlay.probe, environ, fetch, run)

    def read(self, now: datetime) -> ModelDevDeployFacts:
        merges, merges_error = self._merges.read(now)
        receipts, receipts_error = self._receipts.read()
        settled = sorted(
            (
                m
                for m in merges
                if m.runtime_affecting and now - m.merged_at > self._deadline
            ),
            key=lambda m: m.merged_at,
        )
        probes: tuple[ModelRuntimeProbeFact, ...] = ()
        probes_error: str | None
        if merges_error is not None:
            probes_error = f"no sha to expect: merges unreadable ({merges_error})"
        elif not settled:
            probes_error = (
                "no settled runtime-affecting merge in the lookback to expect"
            )
        else:
            probes, probes_error = self._probe.read(now, settled[-1].sha)
        return ModelDevDeployFacts(
            merges=merges,
            receipts=receipts,
            probes=probes,
            receipts_unread_reason=receipts_error
            or (merges_error and f"merges unreadable ({merges_error})"),
            probes_unread_reason=probes_error,
        )


__all__ = [
    "GitRuntimeMergeSource",
    "LabDevDeployFacts",
    "LaneHealthReceiptSource",
    "RuntimeProbeSource",
    "glob_regex",
]
