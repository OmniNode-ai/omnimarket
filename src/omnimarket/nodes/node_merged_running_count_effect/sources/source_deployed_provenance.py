# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Evidence kind deployed from the running build's own provenance file (OMN-22549).

The runtime host's build writes a provenance file naming the commit of each repository it was built from. A merge
whose commit is in the history of that ref is in the running build, so it runs. This reader asks the runtime host
for the file over ssh, and for each declared repository reads the history of its deployed ref in the registry's
clone. It never decides on its own: a file or a clone it cannot read is an unread cause, and that repository's
merges stay without this evidence.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_merged_running_count import (
    ModelMrcEvidence,
    ModelMrcEvidenceRead,
)
from omnimarket.models.model_runtime_host_reads import ModelRuntimeHostReadOverlay

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]
Ancestry = Callable[[Path, str, int], frozenset[str]]


class ModelProvenanceLabels(BaseModel):
    """The evidence kind and source wording of the reader, from the effect's contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str = Field(min_length=1)
    source_template: str = Field(min_length=1)
    git_timeout_seconds: int = Field(gt=0)


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this reader; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


def commits_reachable(clone: Path, ref: str, timeout: int) -> frozenset[str]:
    """Every commit in the history of ``ref`` in ``clone``; a ref the clone lacks raises ``ValueError``."""
    done = subprocess.run(
        ["git", "-C", str(clone), "rev-list", ref],
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if done.returncode:
        raise ValueError(f"git rev-list {ref[:12]} exited {done.returncode}")
    return frozenset(done.stdout.decode().split())


def _ref_at(document: Mapping[str, Any], path: Sequence[str]) -> str:
    node: Any = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    if not isinstance(node, str) or not node:
        raise KeyError(".".join(path))
    return node


class SourceDeployedProvenance:
    """Names every merge in the history of a running build's recorded ref as deployed evidence."""

    def __init__(
        self,
        *,
        overlay: ModelRuntimeHostReadOverlay,
        labels: ModelProvenanceLabels,
        environ: Mapping[str, str],
        home: Path,
        now: datetime,
        run: Runner = run_process,
        ancestry: Ancestry = commits_reachable,
    ) -> None:
        self._overlay = overlay
        self._labels = labels
        self._environ = environ
        self._home = home
        self._now = now
        self._run = run
        self._ancestry = ancestry

    def _document(self) -> dict[str, Any]:
        overlay = self._overlay
        host = self._environ.get(overlay.ssh_host_env, "").strip()
        if not host:
            raise ValueError(f"{overlay.ssh_host_env} is not set")
        script = "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={overlay.compose_project} --filter "
                f"label=com.docker.compose.service={overlay.provenance_service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" cat {overlay.provenance_path}',
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
            overlay.timeout_seconds,
        )
        if done.returncode:
            raise ValueError(f"the provenance read exited {done.returncode}")
        document = json.loads(done.stdout)
        if not isinstance(document, dict):
            raise ValueError("the provenance file is not an object")
        return document

    def read(self, merges: Iterable[tuple[str, int, str]]) -> ModelMrcEvidenceRead:
        """``merges`` are (repository, number, merge sha) of the merged pull requests the count looks at."""
        try:
            document = self._document()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return ModelMrcEvidenceRead(
                unread_cause=f"build provenance unreadable: {type(exc).__name__}: {exc}"[
                    :200
                ]
            )
        built = document.get("build_time")
        try:
            observed = (
                datetime.fromisoformat(str(built).replace("Z", "+00:00")).astimezone(
                    UTC
                )
                if built
                else self._now
            )
        except ValueError:
            observed = self._now
        pending = [(r.rsplit("/", 1)[-1], n, sha) for r, n, sha in merges if sha]
        evidence: list[ModelMrcEvidence] = []
        causes: list[str] = []
        for entry in self._overlay.repo_refs:
            try:
                ref = _ref_at(document, entry.ref_path)
                reachable = self._ancestry(
                    self._home / entry.clone_relpath,
                    ref,
                    self._labels.git_timeout_seconds,
                )
            except KeyError as exc:
                causes.append(f"{entry.repo}: no deployed ref at {exc.args[0]}")
                continue
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                causes.append(
                    f"{entry.repo}: history of the deployed ref unreadable ({exc})"
                )
                continue
            source = self._labels.source_template.format(repo=entry.repo, ref=ref[:12])
            evidence.extend(
                ModelMrcEvidence(
                    repo=entry.repo,
                    kind=self._labels.kind,
                    source=source,
                    number=number,
                    merge_sha=sha,
                    observed_at=observed,
                )
                for repo, number, sha in pending
                if repo == entry.repo and sha in reachable
            )
        return ModelMrcEvidenceRead(
            evidence=tuple(evidence), unread_cause="; ".join(causes)[:300]
        )
