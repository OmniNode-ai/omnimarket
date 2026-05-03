# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Delegation + cost savings demo CLI.

This CLI renders the conference demo spine:

    delegation decision -> LLM cost projection -> savings projection -> joined row

It intentionally uses the production projection handlers with an in-memory
projection adapter. That keeps the proof deterministic and schema-accurate
without requiring a live broker or database for the first pass.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict

import click
import yaml

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_projection_llm_cost.handlers.handler_projection_llm_cost import (
    HandlerProjectionLlmCost,
    ModelLlmCallCompletedEvent,
)
from omnimarket.nodes.node_projection_savings.handlers.handler_projection_savings import (
    HandlerProjectionSavings,
    ModelSavingsEstimatedEvent,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

_DEFAULT_PROFILE_SET: dict[str, Any] = {
    "profiles": {
        "local_201": {
            "local": {
                "delegate_label": "local-qwen",
                "model_id": "qwen3-coder-30b",
                "model_env": "LLM_CODER_MODEL_NAME",
                "source_label": "local/.201",
                "marginal_cost_usd": "0.000000",
                "usage_source": "MEASURED",
            },
            "cloud_baseline": {
                "model_id": "glm-4.5",
                "model_env": "LLM_GLM_MODEL_NAME",
                "source_label": "cloud/z.ai",
            },
        },
        "laptop_standalone": {
            "local": {
                "delegate_label": "laptop-local",
                "model_id": "__SET_ON_LAPTOP__",
                "model_env": "LLM_CODER_MODEL_NAME",
                "source_label": "local/laptop",
                "marginal_cost_usd": "0.000000",
                "usage_source": "MEASURED",
            },
            "cloud_baseline": {
                "model_id": "glm-4.5",
                "model_env": "LLM_GLM_MODEL_NAME",
                "source_label": "cloud/z.ai",
            },
        },
    }
}
_DEFAULT_TASK_TEXT = (
    "Route one ticket-classification task to the local model profile, compare "
    "against the GLM cloud baseline, and materialize the savings projection."
)


@dataclass(frozen=True)
class ResolvedDemoProfile:
    """Resolved model-routing profile for one demo run."""

    name: str
    local_delegate: str
    local_model_id: str
    cloud_baseline_model: str
    local_cost_usd: Decimal
    cloud_cost_usd: Decimal
    savings_usd: Decimal
    usage_source: str


class ProjectionTableCounts(TypedDict):
    """Projection row counts emitted by the proof flow."""

    delegation_events: int
    llm_cost_aggregates: int
    savings_estimates: int


class JoinedProjectionProof(TypedDict):
    """Joined demo row across delegation, cost, and savings projections."""

    correlation_id: object
    task_type: object
    delegated_to: object
    model: object
    tokens: int
    local_cost_usd: object
    cloud_baseline: object
    cloud_cost_usd: object
    savings_usd: object
    usage_source: object
    tables: ProjectionTableCounts


def _candidate_profiles_file() -> Path | None:
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        path = Path(omni_home) / "docs/tracking/2026-05-03-demo-model-profiles.yaml"
        if path.exists():
            return path

    for parent in [Path.cwd(), *Path.cwd().parents]:
        path = parent / "docs/tracking/2026-05-03-demo-model-profiles.yaml"
        if path.exists():
            return path
    return None


def _load_profile_set(profiles_file: Path | None) -> dict[str, Any]:
    path = profiles_file or _candidate_profiles_file()
    if path is None:
        return _DEFAULT_PROFILE_SET

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        msg = f"Model profiles YAML is not readable: {path}: {exc}"
        raise click.ClickException(msg) from exc
    if not isinstance(raw, dict):
        raise click.ClickException(f"Model profiles YAML must be a mapping: {path}")
    return raw


def _profile_entry(profile_set: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = profile_set.get("profiles")
    if not isinstance(profiles, dict):
        raise click.ClickException("Model profile set must contain a profiles mapping.")
    entry = profiles.get(profile_name)
    if not isinstance(entry, dict):
        available = ", ".join(sorted(str(name) for name in profiles))
        raise click.ClickException(
            f"Unknown model profile {profile_name!r}. Available: {available}"
        )
    return entry


def _section(entry: dict[str, Any], name: str) -> dict[str, Any]:
    raw = entry.get(name)
    if not isinstance(raw, dict):
        raise click.ClickException(f"Model profile section {name!r} must be a mapping.")
    return raw


def _decimal(value: str | Decimal | float | int, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception as exc:
        raise click.BadParameter(
            f"{field_name} must be decimal-compatible.", param_hint=field_name
        ) from exc


def _string(value: Any, *, field_name: str) -> str:
    if value is None:
        raise click.ClickException(f"{field_name} must be non-empty.")
    text = str(value).strip()
    if not text:
        raise click.ClickException(f"{field_name} must be non-empty.")
    return text


def _usage_source(value: Any, *, field_name: str) -> str:
    text = _string(value, field_name=field_name)
    try:
        return EnumUsageSource(text).value
    except ValueError:
        try:
            return EnumUsageSource(text.lower()).value
        except ValueError as exc:
            allowed = ", ".join(source.value for source in EnumUsageSource)
            raise click.ClickException(
                f"{field_name} must be one of: {allowed}."
            ) from exc


def _resolve_model_id(
    section: dict[str, Any],
    *,
    override: str | None,
    section_name: str,
) -> str:
    if override:
        return override

    configured = _string(section.get("model_id"), field_name=f"{section_name}.model_id")
    if configured != "__SET_ON_LAPTOP__":
        return configured

    env_name = str(section.get("model_env") or "").strip()
    env_value = os.environ.get(env_name, "").strip() if env_name else ""
    if env_value:
        return env_value

    raise click.ClickException(
        f"{section_name}.model_id is __SET_ON_LAPTOP__; pass an override or set "
        f"{env_name or 'the configured model env var'}."
    )


def _resolve_profile(
    *,
    profile_set: dict[str, Any],
    profile_name: str,
    local_model_id: str | None,
    cloud_baseline_model: str | None,
    local_cost_usd: Decimal | None,
    cloud_cost_usd: Decimal,
) -> ResolvedDemoProfile:
    entry = _profile_entry(profile_set, profile_name)
    local = _section(entry, "local")
    cloud = _section(entry, "cloud_baseline")

    resolved_local_cost = local_cost_usd
    if resolved_local_cost is None:
        resolved_local_cost = _decimal(
            local.get("marginal_cost_usd", "0.000000"),
            field_name="local.marginal_cost_usd",
        )

    if resolved_local_cost < Decimal("0") or cloud_cost_usd < Decimal("0"):
        raise click.ClickException("local_cost_usd and cloud_cost_usd must be >= 0.")

    savings = cloud_cost_usd - resolved_local_cost
    if savings < Decimal("0"):
        raise click.ClickException("cloud_cost_usd must be >= local_cost_usd.")

    return ResolvedDemoProfile(
        name=profile_name,
        local_delegate=_string(
            local.get("delegate_label"), field_name="local.delegate_label"
        ),
        local_model_id=_resolve_model_id(
            local,
            override=local_model_id,
            section_name="local",
        ),
        cloud_baseline_model=_resolve_model_id(
            cloud,
            override=cloud_baseline_model,
            section_name="cloud_baseline",
        ),
        local_cost_usd=resolved_local_cost,
        cloud_cost_usd=cloud_cost_usd,
        savings_usd=savings,
        usage_source=_usage_source(
            local.get("usage_source", "MEASURED"), field_name="local.usage_source"
        ),
    )


def _parse_timestamp(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise click.BadParameter(
            "timestamp must be ISO-8601.", param_hint="--timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise click.BadParameter(
            "timestamp must include a timezone.", param_hint="--timestamp"
        )
    return parsed.astimezone(UTC)


def _now_utc() -> datetime:
    return datetime.now(tz=UTC).replace(microsecond=0)


def _project_flow(
    *,
    profile: ResolvedDemoProfile,
    correlation_id: str,
    session_id: str,
    task_type: str,
    repo: str,
    timestamp: datetime,
    total_tokens: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    machine_id: str,
) -> tuple[
    dict[str, object],
    dict[str, object],
    dict[str, object],
    JoinedProjectionProof,
]:
    db = InmemoryDatabaseAdapter()

    delegation_event = ModelTaskDelegatedEvent(
        correlation_id=correlation_id,
        session_id=session_id,
        task_type=task_type,
        delegated_to=profile.local_delegate,
        model_name=profile.local_model_id,
        delegated_by="omnimarket.routing-policy",
        quality_gate_passed=True,
        quality_gates_checked=["schema", "budget", "latency"],
        quality_gates_failed=[],
        delegation_latency_ms=latency_ms,
        repo=repo,
        is_shadow=False,
        llm_call_id=correlation_id,
        timestamp=timestamp.isoformat(),
    )
    cost_event = ModelLlmCallCompletedEvent(
        call_id=correlation_id,
        model_name=profile.local_model_id,
        session_id=session_id,
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        estimated_cost_usd=float(profile.local_cost_usd),
        usage_source=profile.usage_source,
        timestamp=timestamp.isoformat(),
    )
    savings_event = ModelSavingsEstimatedEvent(
        event_timestamp=timestamp,
        session_id=session_id,
        model_local=profile.local_model_id,
        model_cloud_baseline=profile.cloud_baseline_model,
        local_cost_usd=profile.local_cost_usd,
        cloud_cost_usd=profile.cloud_cost_usd,
        savings_usd=profile.savings_usd,
        repo_name=repo,
        machine_id=machine_id,
    )

    delegation_result = HandlerProjectionDelegation().project(delegation_event, db)
    cost_result = HandlerProjectionLlmCost().project(cost_event, db)
    savings_result = HandlerProjectionSavings().project(savings_event, db)

    delegation_row = db.query("delegation_events", {"correlation_id": correlation_id})[
        0
    ]
    cost_row = db.query("llm_cost_aggregates", {"id": correlation_id})[0]
    savings_row = db.query("savings_estimates", {"session_id": session_id})[0]
    total_tokens_value = cost_row["total_tokens"]
    total_tokens_joined = (
        total_tokens_value
        if isinstance(total_tokens_value, int)
        else int(str(total_tokens_value))
    )

    joined: JoinedProjectionProof = {
        "correlation_id": delegation_row["correlation_id"],
        "task_type": delegation_row["task_type"],
        "delegated_to": delegation_row["delegated_to"],
        "model": cost_row["model_name"],
        "tokens": total_tokens_joined,
        "local_cost_usd": savings_row["local_cost_usd"],
        "cloud_baseline": savings_row["model_cloud_baseline"],
        "cloud_cost_usd": savings_row["cloud_cost_usd"],
        "savings_usd": savings_row["savings_usd"],
        "usage_source": cost_row["usage_source"],
        "tables": {
            "delegation_events": delegation_result.rows_upserted,
            "llm_cost_aggregates": cost_result.rows_upserted,
            "savings_estimates": savings_result.rows_upserted,
        },
    }
    return delegation_row, cost_row, savings_row, joined


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _render_json(
    *,
    profile: ResolvedDemoProfile,
    task_text: str,
    delegation_row: dict[str, object],
    cost_row: dict[str, object],
    savings_row: dict[str, object],
    joined: JoinedProjectionProof,
) -> None:
    payload = {
        "profile": {
            "name": profile.name,
            "local_delegate": profile.local_delegate,
            "local_model_id": profile.local_model_id,
            "cloud_baseline_model": profile.cloud_baseline_model,
        },
        "task_text": task_text,
        "rows": {
            "delegation_events": delegation_row,
            "llm_cost_aggregates": cost_row,
            "savings_estimates": savings_row,
        },
        "joined": joined,
    }
    click.echo(json.dumps(payload, indent=2, default=_json_default))


def _render_table(
    *, profile: ResolvedDemoProfile, task_text: str, joined: JoinedProjectionProof
) -> None:
    header = (
        f"{'correlation_id':<36} {'delegated_to':<14} {'model':<22} "
        f"{'tokens':>8} {'local_cost':>12} {'cloud_base':<14} {'savings':>12}"
    )
    click.echo("DELEGATION COST SAVINGS PROOF")
    click.echo(f"profile={profile.name}")
    click.echo(f"task={task_text}")
    click.echo(header)
    click.echo("-" * len(header))
    click.echo(
        f"{joined['correlation_id']!s:<36} "
        f"{joined['delegated_to']!s:<14} "
        f"{joined['model']!s:<22} "
        f"{joined['tokens']:>8} "
        f"${Decimal(str(joined['local_cost_usd'])):>11.6f} "
        f"{joined['cloud_baseline']!s:<14} "
        f"${Decimal(str(joined['savings_usd'])):>11.6f}"
    )
    click.echo(
        "projection_rows="
        f"delegation_events:{joined['tables']['delegation_events']} "
        f"llm_cost_aggregates:{joined['tables']['llm_cost_aggregates']} "
        f"savings_estimates:{joined['tables']['savings_estimates']}"
    )
    click.echo(
        "usage_source="
        f"{joined['usage_source']} local_model={profile.local_model_id} "
        f"cloud_baseline={profile.cloud_baseline_model}"
    )


def _run(
    *,
    profile_name: str,
    profiles_file: Path | None,
    correlation_id: str,
    session_id: str,
    task_type: str,
    task_text: str,
    repo: str,
    timestamp_raw: str | None,
    total_tokens: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    local_model_id: str | None,
    cloud_baseline_model: str | None,
    local_cost_usd_raw: str | None,
    cloud_cost_usd_raw: str,
    machine_id: str,
    output: str,
) -> int:
    if prompt_tokens + completion_tokens != total_tokens:
        raise click.ClickException(
            "prompt_tokens + completion_tokens must equal total_tokens."
        )

    profile_set = _load_profile_set(profiles_file)
    local_cost = (
        _decimal(local_cost_usd_raw, field_name="local_cost_usd")
        if local_cost_usd_raw is not None
        else None
    )
    profile = _resolve_profile(
        profile_set=profile_set,
        profile_name=profile_name,
        local_model_id=local_model_id,
        cloud_baseline_model=cloud_baseline_model,
        local_cost_usd=local_cost,
        cloud_cost_usd=_decimal(cloud_cost_usd_raw, field_name="cloud_cost_usd"),
    )
    timestamp = _parse_timestamp(timestamp_raw) if timestamp_raw else _now_utc()

    delegation_row, cost_row, savings_row, joined = _project_flow(
        profile=profile,
        correlation_id=correlation_id,
        session_id=session_id,
        task_type=task_type,
        repo=repo,
        timestamp=timestamp,
        total_tokens=total_tokens,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
        machine_id=machine_id,
    )

    if output == "json":
        _render_json(
            profile=profile,
            task_text=task_text,
            delegation_row=delegation_row,
            cost_row=cost_row,
            savings_row=savings_row,
            joined=joined,
        )
    else:
        _render_table(profile=profile, task_text=task_text, joined=joined)
    return 0


@click.command()
@click.option("--profile", "profile_name", default="local_201", show_default=True)
@click.option(
    "--profiles-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="YAML model profile file. Defaults to OMNI_HOME docs profile if present.",
)
@click.option(
    "--correlation-id",
    default="demo-2026-05-03-cost-routing-001",
    show_default=True,
)
@click.option(
    "--session-id",
    default="conference-demo-2026-05-03",
    show_default=True,
)
@click.option("--task-type", default="ticket-classification", show_default=True)
@click.option("--task-text", default=_DEFAULT_TASK_TEXT, show_default=True)
@click.option("--repo", default="omnimarket", show_default=True)
@click.option("--timestamp", "timestamp_raw", default=None, help="ISO-8601 timestamp.")
@click.option("--total-tokens", default=123, show_default=True, type=int)
@click.option("--prompt-tokens", default=74, show_default=True, type=int)
@click.option("--completion-tokens", default=49, show_default=True, type=int)
@click.option("--latency-ms", default=423, show_default=True, type=int)
@click.option("--local-model-id", default=None, help="Override profile local model id.")
@click.option(
    "--cloud-baseline-model",
    default=None,
    help="Override profile cloud baseline model id.",
)
@click.option(
    "--local-cost-usd",
    default=None,
    help="Override profile local cost. Defaults to profile marginal cost.",
)
@click.option("--cloud-cost-usd", default="0.000084", show_default=True)
@click.option("--machine-id", default="local-demo", show_default=True)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)
def main(
    profile_name: str,
    profiles_file: Path | None,
    correlation_id: str,
    session_id: str,
    task_type: str,
    task_text: str,
    repo: str,
    timestamp_raw: str | None,
    total_tokens: int,
    prompt_tokens: int,
    completion_tokens: int,
    latency_ms: int,
    local_model_id: str | None,
    cloud_baseline_model: str | None,
    local_cost_usd: str | None,
    cloud_cost_usd: str,
    machine_id: str,
    output: str,
) -> None:
    """Project one delegation/cost/savings proof flow and print the joined row."""
    sys.exit(
        _run(
            profile_name=profile_name,
            profiles_file=profiles_file,
            correlation_id=correlation_id,
            session_id=session_id,
            task_type=task_type,
            task_text=task_text,
            repo=repo,
            timestamp_raw=timestamp_raw,
            total_tokens=total_tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            local_model_id=local_model_id,
            cloud_baseline_model=cloud_baseline_model,
            local_cost_usd_raw=local_cost_usd,
            cloud_cost_usd_raw=cloud_cost_usd,
            machine_id=machine_id,
            output=output,
        )
    )


__all__ = ["main"]
