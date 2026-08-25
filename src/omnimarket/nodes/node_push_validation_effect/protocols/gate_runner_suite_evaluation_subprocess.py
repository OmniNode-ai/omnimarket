# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SuiteEvaluationGateContainerSubprocess — the R1 execution seam (OMN-16524).

Runs the WHOLE checkout-and-suite sequence via `docker exec` into the
already-deployed bounded gate-runner container (OMN-16295,
`docker/docker-compose.gate-runner.yml` @ `d36b7ccba` — `cpus: "4.0"`,
`mem_limit: 8g`, `memswap_limit: 12g`, `pids_limit: 8192`) — reused, not
re-derived. This module issues no `docker run`/`compose up`; it assumes the
container is already up (it is, per the OMN-16295 deployment) and only
`exec`s into it, so it never mutates the container's own lifecycle.

Distinct execution-isolation choice from `GitPushValidationSubprocess`
(OMN-14920), which runs `uv run pytest` as a plain host/runtime-container
subprocess with no gate-runner container involved at all — that operation
never carried this rung's bounded-execution requirement. R1's ticket
requires the bounded gate-runner container class specifically; this module
exists because that requirement cannot be satisfied by copying the sibling
client unchanged.

Workroot: a DEDICATED clone tree under `${ONEX_SUITE_EVAL_WORKROOT}`
(fail-fast `KeyError` when unset, no silent default), distinct from BOTH
`ONEX_PUSH_VALIDATION_WORKROOT` and any canonical `omni_home/<repo>` clone —
this operation only ever reads (checkout + pytest), but a shared or
canonical clone would still be a mutation hazard (detached-HEAD checkouts
racing other sessions' worktrees), so it gets its own tree.

Reachability note (named, not hidden): this module talks to the gate-runner
container via `docker exec` from wherever ITS OWN process runs. The
CURRENTLY-DEPLOYED `.201` dev-lane runtime-worker container has no Docker
socket and no SSH loopback configured to reach a sibling container — a real,
separately-tracked gap distinct from this module's own correctness. See the
PR body / ledger for the live-proof execution path used to work around it
for R1 (host-run invocation, matching `GitPushValidationSubprocess`'s own
"host-run, not containerized" precedent) and the residual ticket recommended
for wiring live bus dispatch through the containerized runtime.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import socket
import subprocess

from omnimarket.nodes.node_push_validation_effect.protocols.protocol_push_validation_client import (
    ModelSuiteRun,
)
from omnimarket.nodes.node_push_validation_effect.protocols.protocol_suite_evaluation_client import (
    ModelSuiteEvaluationResult,
)

_WORKROOT_ENV = "ONEX_SUITE_EVAL_WORKROOT"
_GATE_RUNNER_CONTAINER_ENV = "ONEX_GATE_RUNNER_CONTAINER"
_DEFAULT_GATE_RUNNER_CONTAINER = "omninode-gate-runner"
_DOCKER_EXEC_TIMEOUT_SECONDS = 300.0
# Suite runs are multi-minute CPU-saturating jobs bounded by the gate-runner
# container's own 4-CPU cap; generous but not unbounded.
_SUITE_TIMEOUT_SECONDS = 14400.0


class SuiteEvaluationInfraError(RuntimeError):
    """Infrastructure failure — routed to the failure terminal topic."""


def _container_name() -> str:
    return os.environ.get(_GATE_RUNNER_CONTAINER_ENV, _DEFAULT_GATE_RUNNER_CONTAINER)


def _workroot() -> str:
    # Fail-fast KeyError over a silent wrong default (workspace rule #8).
    return os.environ[_WORKROOT_ENV]


def _repo_owner_name(repo: str) -> tuple[str, str]:
    parts = repo.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise SuiteEvaluationInfraError(f"repo is not an owner/name slug: {repo!r}")
    return parts[0], parts[1]


def _docker_exec(script: str, *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one bash script inside the gate-runner container via `docker exec`.

    Assumes the container is already running (OMN-16295's deployed compose
    service); this module never starts, stops, or recreates it.
    """
    return subprocess.run(
        ["docker", "exec", "-i", _container_name(), "bash", "-lc", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_or_raise(script: str, *, timeout: float = _DOCKER_EXEC_TIMEOUT_SECONDS) -> str:
    result = _docker_exec(script, timeout=timeout)
    if result.returncode != 0:
        raise SuiteEvaluationInfraError(
            f"gate-runner exec failed (exit {result.returncode}): "
            f"{result.stderr.strip()[-800:]}"
        )
    return result.stdout.strip()


class SuiteEvaluationGateContainerSubprocess:
    """`docker exec`-backed suite-evaluation client (bounded gate-runner seam)."""

    def evaluate_commit(
        self, repo: str, commit_sha: str, suite_scope: str
    ) -> ModelSuiteEvaluationResult:
        owner, name = _repo_owner_name(repo)
        repo_dir = f"{_workroot()}/{owner}/{name}"
        quoted_dir = shlex.quote(repo_dir)
        quoted_sha = shlex.quote(commit_sha)
        quoted_url = shlex.quote(f"https://github.com/{repo}.git")

        # (1) Clone-if-absent, fetch, verify the commit is a KNOWN object
        # BEFORE any checkout — an unknown SHA is an infra/input error
        # (RAISE), never a domain outcome.
        _run_or_raise(
            f"set -e; mkdir -p {quoted_dir}; "
            f"if [ ! -d {quoted_dir}/.git ]; then git clone {quoted_url} {quoted_dir}; fi; "
            f"cd {quoted_dir} && git fetch origin --quiet && "
            f"git cat-file -e {quoted_sha}^{{commit}}"
        )

        # (2) Detached checkout at EXACTLY commit_sha — never "whatever a
        # branch currently is." Then independently compute the tree digest
        # AFTER checkout, never trusting the caller's claim.
        evaluated_tree_digest = _run_or_raise(
            f"cd {quoted_dir} && git checkout --quiet --detach {quoted_sha} && "
            f"git rev-parse {quoted_sha}^{{tree}}"
        )

        # (3) Selector-policy digest input: the resolved suite_scope plus the
        # evaluated commit's own pyproject.toml bytes when present (empty
        # string when absent — still a real, reproducible input).
        pyproject_result = _docker_exec(
            f"cd {quoted_dir} && cat pyproject.toml 2>/dev/null || true",
            timeout=_DOCKER_EXEC_TIMEOUT_SECONDS,
        )
        pyproject_bytes = pyproject_result.stdout.encode("utf-8")
        selector_policy_digest = hashlib.sha256(
            suite_scope.encode("utf-8") + b"\x00" + pyproject_bytes
        ).hexdigest()

        # (4) Run the suite. A non-zero exit is a DOMAIN outcome (red suite),
        # not an infra error — never raise here.
        quoted_scope = shlex.quote(suite_scope)
        suite_result = _docker_exec(
            f"cd {quoted_dir} && uv run pytest {quoted_scope} -v",
            timeout=_SUITE_TIMEOUT_SECONDS,
        )
        log_text = suite_result.stdout + "\n" + suite_result.stderr
        suite = ModelSuiteRun(
            passed=suite_result.returncode == 0,
            log_digest=hashlib.sha256(log_text.encode("utf-8")).hexdigest(),
            detail="" if suite_result.returncode == 0 else log_text.strip()[-1000:],
        )

        return ModelSuiteEvaluationResult(
            evaluated_tree_digest=evaluated_tree_digest,
            selector_policy_digest=selector_policy_digest,
            suite=suite,
        )

    def read_host_identity(self) -> str:
        # The gate-runner container's own stable hostname (compose sets
        # `hostname: gate-runner-201`); fall back to this process's own
        # hostname if the container is unreachable for some reason so the
        # infra RAISE path above still carries an identity on any partial log.
        result = _docker_exec("hostname -s", timeout=30.0)
        readback = result.stdout.strip()
        if result.returncode == 0 and readback:
            return readback
        return socket.gethostname() or "unknown-host"


__all__ = ["SuiteEvaluationGateContainerSubprocess", "SuiteEvaluationInfraError"]
