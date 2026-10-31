# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the sources, run the pure process-health compute and write its JSON to the contract's path."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.process_health import (
    ModelProcessHealthConfig,
    ModelProcessHealthRequest,
)
from omnimarket.nodes import node_process_health_compute as compute_package
from omnimarket.nodes.node_process_health_compute.handlers.handler_process_health import (
    HandlerProcessHealth,
)
from omnimarket.nodes.node_process_health_effect.handlers.source_readers import (
    Sources,
    UnsetEnvError,
    expand,
    read_git,
    read_landing,
    read_landing_metrics,
    read_ledger,
    read_linear,
    read_repo_status,
)
from omnimarket.nodes.node_process_health_effect.models.model_process_health_run import (
    ModelProcessHealthRunRequest,
    ModelProcessHealthRunResult,
    ModelProcessHealthRunSource,
)

EFFECT_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
COMPUTE_CONTRACT = (
    Path(compute_package.__file__ or "").resolve().parent / "contract.yaml"
)


class OutputPathError(RuntimeError):
    """No output path could be resolved."""


def load_config(path: Path = COMPUTE_CONTRACT) -> ModelProcessHealthConfig:
    """The metric config the compute node's contract declares."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelProcessHealthConfig.model_validate(contract["config"]["process_health"])


def load_runtime_io(path: Path = EFFECT_CONTRACT) -> dict[str, Any]:
    """The runtime_io block of this node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["runtime_io"])


def read_sources(
    config: ModelProcessHealthConfig,
    runtime_io: Mapping[str, Any],
    request: ModelProcessHealthRunRequest,
) -> Sources:
    """Read every source; keep the series window plus the summary's look-back."""
    environ = request.environ
    generated_at = request.generated_at
    series_start = generated_at - timedelta(days=config.series_days + 1)
    summary_start = (
        config.reset_at - timedelta(hours=config.summary_max_hours) - timedelta(days=1)
    )
    keep_from = min(series_start, summary_start)
    git_since = min(
        keep_from, generated_at - timedelta(days=int(runtime_io["git"]["since_days"]))
    )
    out = Sources()
    read_ledger(runtime_io["ledger"], environ, keep_from, out)
    read_landing(
        runtime_io["landing"],
        environ,
        config.fix_dispatch_kind,
        config.fix_dispatch_outcome,
        keep_from,
        out,
    )
    read_repo_status(runtime_io["repo_status"], environ, out)
    read_landing_metrics(runtime_io["landing_metrics_history"], environ, out)
    read_linear(runtime_io["linear_snapshot"], environ, out)
    read_git(runtime_io["git"], environ, git_since, out)
    return out


def build_request(
    config: ModelProcessHealthConfig,
    sources: Sources,
    request: ModelProcessHealthRunRequest,
) -> ModelProcessHealthRequest:
    """The typed compute request from what was read."""
    return ModelProcessHealthRequest(
        generated_at=request.generated_at,
        config=config,
        sources=tuple(sources.reads),
        ledger_rows=tuple(sources.ledger_rows),
        repo_status_events=tuple(sources.repo_status_events),
        merges=tuple(sources.merges),
        decisions=tuple(sources.decisions),
        leases=tuple(sources.leases),
        delegation_rows=tuple(sources.delegation_rows),
        landing_metrics=tuple(sources.landing_metrics),
        linear_issues=tuple(sources.linear_issues),
    )


def resolve_output(
    runtime_io: Mapping[str, Any], request: ModelProcessHealthRunRequest
) -> Path:
    """The explicit override, else the contract's environment variable, else its default."""
    output = runtime_io["output"]
    if request.output_override:
        return Path(request.output_override)
    if request.environ.get(output["env_var"]):
        return Path(request.environ[output["env_var"]])
    try:
        return Path(expand(output["default"], request.environ))
    except UnsetEnvError as exc:
        raise OutputPathError(str(exc)) from exc


def write_output(path: Path, text: str) -> None:
    """Write through a temporary file so a reader never sees half a document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


class HandlerProcessHealthRun:
    """Effect: read the named sources, call the pure compute, write the JSON."""

    def handle(
        self, request: ModelProcessHealthRunRequest
    ) -> ModelProcessHealthRunResult:
        config = load_config()
        runtime_io = load_runtime_io()
        output = resolve_output(runtime_io, request)
        sources = read_sources(config, runtime_io, request)
        result = HandlerProcessHealth().handle(build_request(config, sources, request))
        write_output(output, result.model_dump_json(indent=2) + "\n")
        return ModelProcessHealthRunResult(
            output_path=str(output),
            sources=tuple(
                ModelProcessHealthRunSource(
                    name=r.name, status=r.status, reason=r.reason
                )
                for r in sources.reads
            ),
        )
