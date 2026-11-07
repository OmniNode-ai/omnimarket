# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime's own revision and omnimarket's source tree at that revision (OMN-22787).

Two backends of each port. The in-memory ones answer from given values and are the default for
tests. The real ones read the runtime's build provenance file over ssh (the same file the
merged-and-running count reads) and export the tree with ``git archive`` from an omnimarket clone.
Nothing here decides a verdict: a revision that cannot be read or exported raises
``RuntimeRevisionError`` and the read becomes UNKNOWN with that reason, never a guess.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_overlay_drift_effect.overlay import (
    ModelRuntimeRevisionSource,
)
from omnimarket.nodes.node_overlay_drift_effect.protocols import RuntimeRevisionError

_REVISION = re.compile(r"^[0-9a-f]{12,64}$")

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of these adapters; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


class InMemoryRuntimeRevision:
    """The runtime says a given revision (or nothing, which is an unreadable read)."""

    def __init__(self, revision: str | None) -> None:
        self.revision = revision

    def read(self) -> str:
        if self.revision is None:
            raise RuntimeRevisionError("provenance-unreadable")
        return self.revision


class InMemoryRevisionTree:
    """Source trees by revision: each is a directory that already holds an omnimarket ``src``."""

    def __init__(self, trees: Mapping[str, Path]) -> None:
        self.trees = dict(trees)

    def materialize(self, revision: str, into: Path) -> Path:
        try:
            return self.trees[revision]
        except KeyError:
            raise RuntimeRevisionError("revision-not-in-clone") from None


def _ref_at(document: Mapping[str, Any], path: Sequence[str]) -> str:
    node: Any = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise RuntimeRevisionError("provenance-has-no-ref-at-path")
        node = node[key]
    if not isinstance(node, str) or not _REVISION.match(node):
        raise RuntimeRevisionError("provenance-ref-is-not-a-commit")
    return node


class ProvenanceRuntimeRevision:
    """Read the revision from the build provenance file inside the runtime's container, over ssh."""

    def __init__(
        self,
        source: ModelRuntimeRevisionSource,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._source = source
        self._environ = environ
        self._run = run

    def _script(self) -> str:
        source = self._source
        return "\n".join(
            [
                "set -eu",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={source.compose_project} --filter "
                f"label=com.docker.compose.service={source.service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" cat {source.provenance_path}',
            ]
        )

    def read(self) -> str:
        source = self._source
        host = self._environ.get(source.ssh_host_env, "").strip()
        if not host:
            raise RuntimeRevisionError("ssh-host-variable-unset")
        try:
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
                self._script().encode(),
                source.read_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeRevisionError("provenance-read-timeout") from None
        except OSError as exc:
            raise RuntimeRevisionError(
                f"provenance-read-unstartable-{type(exc).__name__}"
            ) from None
        if done.returncode:
            raise RuntimeRevisionError(f"provenance-read-exit-{done.returncode}")
        try:
            document = json.loads(done.stdout)
        except ValueError:
            raise RuntimeRevisionError("provenance-not-json") from None
        if not isinstance(document, dict):
            raise RuntimeRevisionError("provenance-not-an-object")
        return _ref_at(document, source.ref_path)


class GitRevisionTree:
    """Export ``src`` of a revision from an omnimarket clone with ``git archive``."""

    def __init__(
        self, clone: Path, timeout_seconds: int, run: Runner = run_process
    ) -> None:
        self._clone = clone
        self._timeout = timeout_seconds
        self._run = run

    def materialize(self, revision: str, into: Path) -> Path:
        if not _REVISION.match(revision):
            raise RuntimeRevisionError("revision-is-not-a-commit")
        if not (self._clone / ".git").exists():
            raise RuntimeRevisionError("clone-absent")
        try:
            held = self._run(
                [
                    "git",
                    "-C",
                    str(self._clone),
                    "cat-file",
                    "-e",
                    f"{revision}^{{commit}}",
                ],
                None,
                self._timeout,
            )
            if held.returncode:
                raise RuntimeRevisionError("revision-not-in-clone")
            archive = self._run(
                [
                    "git",
                    "-C",
                    str(self._clone),
                    "archive",
                    "--format=tar",
                    revision,
                    "src",
                ],
                None,
                self._timeout,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeRevisionError("git-timeout") from None
        except OSError as exc:
            raise RuntimeRevisionError(
                f"git-unstartable-{type(exc).__name__}"
            ) from None
        if archive.returncode:
            raise RuntimeRevisionError(f"git-archive-exit-{archive.returncode}")
        try:
            with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
                bundle.extractall(into, filter="data")
        except (tarfile.TarError, OSError):
            raise RuntimeRevisionError("tree-unpack-failed") from None
        return into / "src"


__all__ = [
    "GitRevisionTree",
    "InMemoryRevisionTree",
    "InMemoryRuntimeRevision",
    "ProvenanceRuntimeRevision",
    "run_process",
]
