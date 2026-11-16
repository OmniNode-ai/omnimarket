# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The runtime build probe of node_branch_deploy_probe_effect."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from omnimarket.models.model_dev_deploy_reading import ModelRuntimeProbeFact
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import ModelProbeSettings

Runner = Callable[
    [Sequence[str], bytes | None, int], "subprocess.CompletedProcess[bytes]"
]


def run_process(
    argv: Sequence[str], stdin: bytes | None, timeout: int
) -> subprocess.CompletedProcess[bytes]:
    """The one subprocess boundary of this probe; tests replace it."""
    return subprocess.run(
        list(argv), input=stdin, capture_output=True, timeout=timeout, check=False
    )


def _walk(document: Any, path: Sequence[str]) -> Any:
    node = document
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            raise KeyError(".".join(path))
        node = node[key]
    return node


class RuntimeBuildProbe:
    """Reads the runtime's health and build provenance over one ssh call."""

    def __init__(
        self,
        settings: ModelProbeSettings,
        environ: Mapping[str, str],
        run: Runner = run_process,
    ) -> None:
        self._settings = settings
        self._environ = environ
        self._run = run

    def read(
        self, now: datetime, expected_sha: str
    ) -> tuple[tuple[ModelRuntimeProbeFact, ...], str | None]:
        s = self._settings
        host = self._environ.get(s.ssh_host_env, "").strip()
        if not host:
            return (), "runtime probe unreadable: ValueError"
        script = "\n".join(
            [
                "set -eu",
                f"curl -fsS --max-time {s.timeout_seconds} {shlex.quote(s.health_url)}",
                "echo",
                "echo '---'",
                "RT=$(docker ps --filter "
                f"label=com.docker.compose.project={s.compose_project} --filter "
                f"label=com.docker.compose.service={s.provenance_service} "
                "--filter status=running --format '{{.Names}}' | head -1)",
                '[ -n "$RT" ]',
                f'docker exec -i "$RT" cat {s.provenance_path}',
            ]
        )
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
                script.encode(),
                s.timeout_seconds,
            )
            if done.returncode:
                return (), f"the runtime host read exited {done.returncode}"
            text = done.stdout.decode()
            left, _, right = text.partition("\n---\n")
            if not right:
                raise ValueError("the runtime host read has no provenance section")
            healthy = _walk(json.loads(left), s.health_ok_path)
            observed = _walk(json.loads(right), s.sha_path)
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
