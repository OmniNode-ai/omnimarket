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

from omnimarket.models.model_dev_deploy_reading import (
    ModelDeployAgentFacts,
    ModelDeployAgentRule,
    ModelDeploySource,
    ModelLabPassReceiptFact,
    ModelLineageFact,
    ModelRuntimeMerge,
    ModelRuntimeProbeFact,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDeployAgentSettings,
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


def _parse_git_log(
    text: str, patterns: tuple[re.Pattern[str], ...]
) -> list[tuple[str, datetime, bool]]:
    """Parse the ``--format=\x01%H\t%cI`` log into (sha, committer time, runtime-affecting)."""
    out: list[tuple[str, datetime, bool]] = []
    for chunk in text.split(_START)[1:]:
        head, *files = chunk.split("\n")
        sha, stamp = head.split("\t")
        out.append(
            (
                sha,
                _utc(stamp),
                any(p.fullmatch(f) for f in files if f for p in patterns),
            )
        )
    return out


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
        try:
            parsed = _parse_git_log(done.stdout.decode(), self._patterns)
        except (ValueError, ValidationError) as exc:
            return (), f"git log output unreadable: {type(exc).__name__}"
        return (
            tuple(
                ModelRuntimeMerge(sha=sha, merged_at=stamp, runtime_affecting=affecting)
                for sha, stamp, affecting in parsed
            ),
            None,
        )


class GitLineageSource:
    """The clone's reading of ancestry between two exact shas, and the runtime-affecting commits between them."""

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

    def _clone(self) -> str:
        return self._environ.get(self._settings.clone_path_env, "").strip()

    def pair(self, base_sha: str, head_sha: str) -> ModelLineageFact:
        settings = self._settings
        clone = self._clone()
        if not clone:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"{settings.clone_path_env} is not set",
            )
        try:
            done = self._run(
                [
                    "git",
                    "-C",
                    str(Path(clone)),
                    "merge-base",
                    "--is-ancestor",
                    base_sha,
                    head_sha,
                ],
                None,
                settings.git_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git merge-base failed: {type(exc).__name__}",
            )
        if done.returncode not in (0, 1):
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git merge-base exited {done.returncode}",
            )
        if done.returncode == 1:
            return ModelLineageFact(
                base_sha=base_sha, head_sha=head_sha, relation="not-ancestor"
            )
        try:
            count = self._run(
                [
                    "git",
                    "-C",
                    str(Path(clone)),
                    "rev-list",
                    "--first-parent",
                    "--count",
                    f"{base_sha}..{head_sha}",
                ],
                None,
                settings.git_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git rev-list failed: {type(exc).__name__}",
            )
        if count.returncode:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git rev-list exited {count.returncode}",
            )
        try:
            commits_between = int(count.stdout.decode().strip())
        except ValueError:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason="git rev-list count is not an integer",
            )
        return ModelLineageFact(
            base_sha=base_sha,
            head_sha=head_sha,
            relation="ancestor",
            commits_between=commits_between,
        )

    def pair_with_runtime(self, base_sha: str, head_sha: str) -> ModelLineageFact:
        fact = self.pair(base_sha, head_sha)
        if fact.relation != "ancestor":
            return fact
        settings = self._settings
        clone = self._clone()
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
                    f"--format={_START}%H%x09%cI",
                    f"{base_sha}..{head_sha}",
                    "--",
                ],
                None,
                settings.git_timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git log failed: {type(exc).__name__}",
            )
        if done.returncode:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git log exited {done.returncode}",
            )
        try:
            parsed = _parse_git_log(done.stdout.decode(), self._patterns)
        except (ValueError, ValidationError) as exc:
            return ModelLineageFact(
                base_sha=base_sha,
                head_sha=head_sha,
                relation="unread",
                unread_reason=f"git log output unreadable: {type(exc).__name__}",
            )
        return ModelLineageFact(
            base_sha=base_sha,
            head_sha=head_sha,
            relation="ancestor",
            commits_between=fact.commits_between,
            runtime_affecting_between=tuple(
                sha for sha, _, affecting in parsed if affecting
            ),
        )


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

    def build_record(self) -> dict[str, Any]:
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
            observed = _walk(self.build_record(), s.sha_path)
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


class DeployAgentSource:
    """What the deploy agent itself reports and what the running runtime's build record names (OMN-23655).

    Three read-only reads: the agent's ``/health`` (the job in flight, the newest finished job), its ``/queue`` (the commands
    ahead) and the runtime's build provenance (the commit of the merge's repository, and when the build was made). A half
    that cannot be read is named, never a zero: a queue that could not be read carries its reason and no count.
    """

    def __init__(
        self,
        settings: ModelDeployAgentSettings,
        probe: ModelProbeSettings,
        environ: Mapping[str, str],
        fetch: Fetch = http_get,
        run: Runner = run_process,
    ) -> None:
        self._settings = settings
        self._fetch = fetch
        self._provenance = RuntimeProbeSource(probe, environ, fetch, run)

    def _queue(self) -> tuple[int | None, str | None]:
        s = self._settings
        try:
            document = json.loads(self._fetch(s.queue_url, s.timeout_seconds))
            ahead = document["commands_ahead"]
            reason = document.get("control_topic_lag_reason") or ""
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            return None, f"queue unreadable: {type(exc).__name__}"
        if isinstance(ahead, bool) or not isinstance(ahead, int) or ahead < 0:
            return None, reason or "queue snapshot has no commands_ahead count"
        if reason:
            return None, reason
        return ahead, None

    def read_facts(
        self, provenance_section: str, source_repo: str
    ) -> tuple[ModelDeployAgentFacts | None, str | None]:
        s = self._settings
        try:
            health = json.loads(self._fetch(s.health_url, s.timeout_seconds))
            active = health["active_job"]
            last = health["last_result"]
            document = self._provenance.build_record()
            build_time = _utc(document["build_time"])
            if provenance_section == "siblings":
                running = _walk(
                    document,
                    ("per_repo_vcs_provenance", "siblings", source_repo, "vcs_ref"),
                )
            else:
                running = _walk(document, ("infra_vcs_ref",))
            ahead, ahead_reason = self._queue()
            return (
                ModelDeployAgentFacts(
                    running_sha=running,
                    build_time=build_time,
                    in_flight_job_id=None
                    if active is None
                    else active["correlation_id"],
                    in_flight_started_at=None
                    if active is None
                    else _utc(active["started_at"]),
                    commands_ahead=ahead,
                    commands_ahead_unread_reason=ahead_reason,
                    last_job_id=None if last is None else last["correlation_id"],
                    last_job_status=None if last is None else last["status"],
                    last_job_completed_at=None
                    if last is None
                    else _utc(last["completed_at"]),
                ),
                None,
            )
        except ValidationError:
            return (
                None,
                "deploy agent source unreadable: a fact is not in its declared shape",
            )
        except (
            OSError,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            subprocess.SubprocessError,
        ) as exc:
            return None, f"deploy agent source unreadable: {type(exc).__name__}"

    def source(self, provenance_section: str, source_repo: str) -> ModelDeploySource:
        facts, reason = self.read_facts(provenance_section, source_repo)
        return ModelDeploySource(
            rule=ModelDeployAgentRule(
                idle_grace_seconds=self._settings.idle_grace_seconds
            ),
            facts=facts,
            unread_reason=reason,
        )


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
        self._lineage = GitLineageSource(overlay.merges, environ, run)
        self._lineage_rule = overlay.lineage
        self._required_lane = overlay.thresholds.required_lane
        self._overlay = overlay
        self._deploy = DeployAgentSource(
            overlay.deploy_agent, overlay.probe, environ, fetch, run
        )

    def _read_deploy_agent(self, now: datetime) -> ModelDevDeployFacts:
        """The deploy-agent rule's facts: containment of each runtime-affecting merge in the running build, no receipts read."""
        merges, merges_error = self._merges.read(now)
        agent = self._overlay.deploy_agent
        deploy = self._deploy.source(agent.provenance_section, agent.source_repo)
        lineage: list[ModelLineageFact] = []
        probes: tuple[ModelRuntimeProbeFact, ...] = ()
        probes_error: str | None
        if deploy.facts is None:
            probes_error = f"no running sha to expect: {deploy.unread_reason}"
        else:
            probes, probes_error = self._probe.read(now, deploy.facts.running_sha)
            if merges_error is None:
                lineage = [
                    self._lineage.pair(merge.sha, deploy.facts.running_sha)
                    for merge in merges
                    if merge.runtime_affecting
                ]
        return ModelDevDeployFacts(
            merges=merges,
            receipts=(),
            probes=probes,
            lineage=tuple(lineage),
            receipts_unread_reason=merges_error
            and f"merges unreadable ({merges_error})",
            probes_unread_reason=probes_error,
            deploy_source=deploy,
        )

    def read(self, now: datetime) -> ModelDevDeployFacts:
        if self._overlay.source_rule == "deploy_agent":
            return self._read_deploy_agent(now)
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
        lineage: list[ModelLineageFact] = []
        if self._lineage_rule.receipt_covers_descendant and merges_error is None:
            for merge in merges:
                if not merge.runtime_affecting:
                    continue
                for receipt in receipts:
                    if (
                        receipt.lane == self._required_lane
                        and receipt.result == "PASS"
                        and receipt.sha != merge.sha
                    ):
                        lineage.append(self._lineage.pair(merge.sha, receipt.sha))
        if (
            self._lineage_rule.probe_accepts_descendant
            and probes_error is None
            and probes
        ):
            probe = probes[0]
            if (
                probe.observed_sha is not None
                and probe.observed_sha != probe.expected_sha
            ):
                lineage.append(
                    self._lineage.pair_with_runtime(
                        probe.expected_sha, probe.observed_sha
                    )
                )
        return ModelDevDeployFacts(
            merges=merges,
            receipts=receipts,
            probes=probes,
            lineage=tuple(lineage),
            receipts_unread_reason=receipts_error
            or (merges_error and f"merges unreadable ({merges_error})"),
            probes_unread_reason=probes_error,
        )


__all__ = [
    "DeployAgentSource",
    "GitLineageSource",
    "GitRuntimeMergeSource",
    "LabDevDeployFacts",
    "LaneHealthReceiptSource",
    "RuntimeProbeSource",
    "glob_regex",
]
