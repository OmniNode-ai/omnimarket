# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-project compose check, run as a step of the lane census on the host it reads (glue called from __main__, shaped like the schedule adapter beside it).

The census collector judges lanes; this step reads the same host's docker inventory once and hands it to
``node_compose_project_census_compute``, which judges every compose project against the policy overlay. The
inventory is read with a field-by-field format and never ``{{json .}}``: that form makes the daemon compute every
container's size, which measured 90 s against 0.15 s on a busy lab host (lane_census_inventory.py, OMN-15466). A probe
that cannot read says so (``inventory-unobservable``); it never reads as clean.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.model_compose_project_census import (
    ModelComposeCensusContainer,
    ModelComposeCensusPolicy,
    ModelComposeCensusRequest,
    ModelComposeProjectCheckResult,
)
from omnimarket.nodes.node_compose_project_census_compute.handlers.handler_compose_project_census import (
    HandlerComposeProjectCensus,
)

ComposeRunner = Callable[[Sequence[str], int], "subprocess.CompletedProcess[str]"]

_LABELS = (
    "com.docker.compose.project",
    "com.docker.compose.service",
    "com.docker.compose.project.working_dir",
    "com.docker.compose.project.config_files",
)
FORMAT = "\t".join(
    ["{{.Names}}", "{{.State}}", "{{.Status}}", "{{.Image}}"]
    + [f'{{{{.Label "{label}"}}}}' for label in _LABELS]
)
_FIELDS = 4 + len(_LABELS)


def run_docker(argv: Sequence[str], timeout_s: int) -> subprocess.CompletedProcess[str]:
    """Run docker; a missing binary or a timeout reads as a non-verdict exit code."""
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(list(argv), 127, "", str(exc))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            list(argv), 124, "", f"docker exceeded {timeout_s}s"
        )


class ModelComposeProjectCheckCommand(BaseModel):
    """One run of the step on the host that runs this process."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelComposeCensusPolicy | None
    hostname: str
    home_dir: str
    docker_binary: str = Field(min_length=1)
    timeout_s: int = Field(gt=0)


def _alias_key(name: str) -> str:
    return name.strip().lower().split(".", 1)[0]


class ComposeProjectCheckStep:
    """handle(ModelComposeProjectCheckCommand) -> ModelComposeProjectCheckResult."""

    def __init__(
        self,
        *,
        run: ComposeRunner = run_docker,
        is_dir: Callable[[str], bool] = os.path.isdir,
    ) -> None:
        self._run = run
        self._is_dir = is_dir

    def handle(
        self, request: ModelComposeProjectCheckCommand
    ) -> ModelComposeProjectCheckResult:
        policy = request.policy
        if policy is None:
            return ModelComposeProjectCheckResult(outcome="skipped-no-policy")
        key = _alias_key(request.hostname)
        host = next(
            (
                row
                for row in policy.hosts
                if key in {_alias_key(alias) for alias in row.aliases}
            ),
            None,
        )
        if host is None:
            return ModelComposeProjectCheckResult(
                outcome="host-unmatched",
                diagnostics=(f"hostname {request.hostname!r} matches no policy host",),
            )
        done = self._run(
            [request.docker_binary, "ps", "-a", "--no-trunc", "--format", FORMAT],
            request.timeout_s,
        )
        if done.returncode != 0:
            return ModelComposeProjectCheckResult(
                outcome="inventory-unobservable",
                host=host.name,
                diagnostics=(
                    f"docker exited {done.returncode}",
                    *(
                        line
                        for line in (done.stderr or "").splitlines()
                        if line.strip()
                    ),
                ),
            )
        containers: list[ModelComposeCensusContainer] = []
        for line in (done.stdout or "").splitlines():
            if not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) != _FIELDS:
                return ModelComposeProjectCheckResult(
                    outcome="inventory-unobservable",
                    host=host.name,
                    diagnostics=(
                        f"inventory line has {len(fields)} fields, expected {_FIELDS}",
                    ),
                )
            labels = {
                label: value
                for label, value in zip(_LABELS, fields[4:], strict=True)
                if value
            }
            containers.append(
                ModelComposeCensusContainer(
                    name=fields[0], state=fields[1], image=fields[3], labels=labels
                )
            )
        absent = sorted(
            {
                directory
                for c in containers
                if (directory := c.labels.get("com.docker.compose.project.working_dir"))
                and not self._is_dir(directory)
            }
        )
        verdict = HandlerComposeProjectCensus().handle(
            ModelComposeCensusRequest(
                host=host.name,
                containers=tuple(containers),
                absent_paths=tuple(absent),
                home_dir=request.home_dir,
                policy=policy,
            )
        )
        return ModelComposeProjectCheckResult(
            outcome="clean" if verdict.clean else "drift",
            host=host.name,
            findings=verdict.findings,
        )
