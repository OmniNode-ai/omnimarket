# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-run AB compare demo CLI."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
import uuid
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import click
import httpx

if TYPE_CHECKING:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        _ResolvedModel,
    )
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
        ModelAbCompareResult,
        ModelComparisonRow,
    )

_DEFAULT_TASKS: tuple[str, ...] = (
    "Write a Python function slugify(text: str) -> str that lowercases text, "
    "replaces non-alphanumeric runs with hyphens, and strips leading/trailing hyphens.",
    "Write a Python function parse_ints(lines: list[str]) -> list[int] that extracts "
    "the first signed integer from each line and skips lines without one.",
)
_GLM_COST_PER_1K_TOTAL_TOKENS = 0.0005


@dataclass(frozen=True)
class ModelAggregate:
    """Aggregate one model across several comparison runs."""

    model_key: str
    display_name: str
    runs: int
    successes: int
    errors: int
    total_tokens: int
    total_cost_usd: float
    avg_latency_ms: float


@dataclass
class _AggregateBucket:
    """Mutable accumulator for one model's aggregate row."""

    model_key: str
    display_name: str
    runs: int = 0
    successes: int = 0
    errors: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    latency_sum_ms: int = 0


def _split_models(models: str) -> list[str]:
    if models.strip().lower() == "all":
        return ["all"]
    model_list = [model for model in (m.strip() for m in models.split(",")) if model]
    if not model_list:
        raise click.BadParameter(
            "At least one model ID is required.", param_hint="--models"
        )
    return model_list


def _load_tasks(tasks: tuple[str, ...], tasks_file: Path | None) -> list[str]:
    loaded: list[str] = []
    if tasks_file is not None:
        for line in tasks_file.read_text(encoding="utf-8").splitlines():
            text = line.strip()
            if text and not text.startswith("#"):
                loaded.append(text)
    loaded.extend(task.strip() for task in tasks if task.strip())
    if not loaded:
        loaded.extend(_DEFAULT_TASKS)
    return loaded


def _curl_post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    command = [
        "curl",
        "-sS",
        "--fail-with-body",
        "--connect-timeout",
        "5",
        "--max-time",
        "120",
        "-H",
        "Content-Type: application/json",
        "-d",
        "@-",
        url,
    ]
    result = subprocess.run(
        command,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=125,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"curl exited {result.returncode}: {detail[:300]}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict):
        raise TypeError("curl response JSON was not an object")
    return data


async def _run_suite_async(
    *,
    tasks: list[str],
    models: list[str],
    system_prompt: str | None,
    quality_check: bool,
) -> list[ModelAbCompareResult]:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        HandlerAbCompareOrchestrator,
    )
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
        ModelAbCompareStart,
    )

    handler = HandlerAbCompareOrchestrator()
    results: list[ModelAbCompareResult] = []
    for index, task in enumerate(tasks, start=1):
        command = ModelAbCompareStart(
            task=task,
            models=models,
            correlation_id=f"ab-suite-{index}-{uuid.uuid4()}",
            system_prompt=system_prompt,
            quality_check=quality_check,
        )
        results.append(await handler.handle(command))
    return results


async def _call_registry_model_direct(
    *,
    model: _ResolvedModel,
    task: str,
    system_prompt: str,
    quality_check: bool,
    max_tokens: int,
) -> ModelComparisonRow:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        _calculate_cost,
        _run_quality_check,
    )
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
        ModelComparisonRow,
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]
    payload: dict[str, Any] = {
        "model": model.model_id_resolved,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    started = time.monotonic()
    url = f"{model.endpoint_url.rstrip('/')}/v1/chat/completions"
    data: dict[str, Any]
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                url,
                json=payload,
                headers=(
                    {"Authorization": f"Bearer {model.api_key}"}
                    if model.api_key
                    else None
                ),
            )
            response.raise_for_status()
            raw_data = response.json()
            if not isinstance(raw_data, dict):
                latency_ms = int((time.monotonic() - started) * 1000)
                return ModelComparisonRow(
                    model_key=model.model_id,
                    display_name=model.display_name,
                    latency_ms=latency_ms,
                    error="InvalidResponse: unexpected body type",
                )
            data = raw_data
    except httpx.ConnectError:
        if model.api_key:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ModelComparisonRow(
                model_key=model.model_id,
                display_name=model.display_name,
                latency_ms=latency_ms,
                error="ConnectError: direct HTTP connection failed",
            )
        try:
            data = _curl_post_json(url, payload)
        except Exception as exc:
            latency_ms = int((time.monotonic() - started) * 1000)
            return ModelComparisonRow(
                model_key=model.model_id,
                display_name=model.display_name,
                latency_ms=latency_ms,
                error=f"{type(exc).__name__}: {exc}",
            )
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ModelComparisonRow(
            model_key=model.model_id,
            display_name=model.display_name,
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    choices = cast(object, data.get("choices"))
    if not isinstance(choices, list) or not choices:
        return ModelComparisonRow(
            model_key=model.model_id,
            display_name=model.display_name,
            latency_ms=latency_ms,
            error="InvalidResponse: missing choices",
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        return ModelComparisonRow(
            model_key=model.model_id,
            display_name=model.display_name,
            latency_ms=latency_ms,
            error="InvalidResponse: malformed choice",
        )

    usage = cast(dict[str, Any], data.get("usage") or {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    message = choice.get("message") or {}
    generated_text = str(message.get("content") or choice.get("text") or "")
    quality = (
        _run_quality_check(generated_text) if quality_check and generated_text else ""
    )

    return ModelComparisonRow(
        model_key=model.model_id,
        display_name=model.display_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=_calculate_cost(model, prompt_tokens, completion_tokens),
        latency_ms=latency_ms,
        quality=quality,
    )


async def _run_direct_suite_async(
    *,
    tasks: list[str],
    models: list[str],
    system_prompt: str | None,
    quality_check: bool,
    include_glm: bool,
    max_tokens: int,
) -> list[ModelAbCompareResult]:
    from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
        _DEFAULT_SYSTEM_PROMPT,
        _load_registry,
        _resolve_models,
    )
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
        ModelAbCompareResult,
    )

    resolved, skipped = _resolve_models(_load_registry(), models)
    prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
    results: list[ModelAbCompareResult] = []

    for index, task in enumerate(tasks, start=1):
        correlation_id = f"ab-suite-{index}-{uuid.uuid4()}"
        rows = await asyncio.gather(
            *(
                _call_registry_model_direct(
                    model=model,
                    task=task,
                    system_prompt=prompt,
                    quality_check=quality_check,
                    max_tokens=max_tokens,
                )
                for model in resolved
            )
        )
        run_skipped = list(skipped)
        if include_glm:
            glm_row = await _call_glm(
                task=task,
                system_prompt=prompt,
                correlation_id=correlation_id,
                max_tokens=max_tokens,
            )
            if glm_row is None:
                run_skipped.append("glm-4.5 (missing LLM_GLM_URL or LLM_GLM_API_KEY)")
            else:
                rows.append(glm_row)
        rows.sort(key=lambda row: (row.error != "", row.cost_usd, row.latency_ms))
        results.append(
            ModelAbCompareResult(
                comparison=rows,
                correlation_id=correlation_id,
                status="COMPLETED" if not run_skipped else "PARTIAL",
                models_skipped=run_skipped,
            )
        )

    return results


async def _call_glm(
    *,
    task: str,
    system_prompt: str | None,
    correlation_id: str,
    max_tokens: int = 512,
) -> ModelComparisonRow | None:
    from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
        ModelComparisonRow,
    )

    base_url = os.environ.get("LLM_GLM_URL", "").strip()
    api_key = os.environ.get("LLM_GLM_API_KEY", "").strip()
    model_name = os.environ.get("LLM_GLM_MODEL_NAME", "").strip()
    if not base_url or not api_key:
        return None

    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": task})
    payload = {
        "model": model_name,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            response.raise_for_status()
            raw_data = response.json()
            if not isinstance(raw_data, dict):
                latency_ms = int((time.monotonic() - started) * 1000)
                return ModelComparisonRow(
                    model_key=model_name,
                    display_name=f"{model_name} (z.ai)",
                    latency_ms=latency_ms,
                    error="InvalidResponse: unexpected body type",
                )
            data = raw_data
    except Exception as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return ModelComparisonRow(
            model_key=model_name,
            display_name=f"{model_name} (z.ai)",
            latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )

    latency_ms = int((time.monotonic() - started) * 1000)
    choices = cast(object, data.get("choices"))
    if not isinstance(choices, list) or not choices:
        return ModelComparisonRow(
            model_key=model_name,
            display_name=f"{model_name} (z.ai)",
            latency_ms=latency_ms,
            error="InvalidResponse: missing choices",
        )
    usage = cast(dict[str, Any], data.get("usage") or {})
    prompt_tokens = int(usage.get("prompt_tokens", 0))
    completion_tokens = int(usage.get("completion_tokens", 0))
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens))
    return ModelComparisonRow(
        model_key=model_name,
        display_name=f"{model_name} (z.ai)",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=(total_tokens * _GLM_COST_PER_1K_TOTAL_TOKENS) / 1000.0,
        latency_ms=latency_ms,
    )


async def _add_glm_rows(
    *,
    tasks: list[str],
    results: list[ModelAbCompareResult],
    system_prompt: str | None,
) -> list[ModelAbCompareResult]:
    enriched: list[ModelAbCompareResult] = []
    for task, result in zip(tasks, results, strict=True):
        row = await _call_glm(
            task=task,
            system_prompt=system_prompt,
            correlation_id=result.correlation_id,
        )
        if row is None:
            enriched.append(
                result.model_copy(
                    update={
                        "status": "PARTIAL",
                        "models_skipped": [
                            *result.models_skipped,
                            "glm-4.5 (missing LLM_GLM_URL or LLM_GLM_API_KEY)",
                        ],
                    }
                )
            )
            continue
        enriched.append(
            result.model_copy(update={"comparison": [*result.comparison, row]})
        )
    return enriched


def _run_suite(
    *,
    tasks: list[str],
    models: list[str],
    system_prompt: str | None,
    quality_check: bool,
    include_glm: bool,
    transport: str,
    max_tokens: int,
) -> list[ModelAbCompareResult]:
    async def _run() -> list[ModelAbCompareResult]:
        if transport == "direct":
            return await _run_direct_suite_async(
                tasks=tasks,
                models=models,
                system_prompt=system_prompt,
                quality_check=quality_check,
                include_glm=include_glm,
                max_tokens=max_tokens,
            )
        results = await _run_suite_async(
            tasks=tasks,
            models=models,
            system_prompt=system_prompt,
            quality_check=quality_check,
        )
        if include_glm:
            from omnimarket.nodes.node_ab_compare_orchestrator.handlers.handler_ab_compare_orchestrator import (
                _DEFAULT_SYSTEM_PROMPT,
            )

            results = await _add_glm_rows(
                tasks=tasks,
                results=results,
                system_prompt=system_prompt or _DEFAULT_SYSTEM_PROMPT,
            )
        return results

    return asyncio.run(_run())


def _aggregate(results: list[ModelAbCompareResult]) -> list[ModelAggregate]:
    buckets: dict[str, _AggregateBucket] = {}
    for result in results:
        for row in result.comparison:
            bucket = buckets.setdefault(
                row.model_key,
                _AggregateBucket(
                    model_key=row.model_key,
                    display_name=row.display_name,
                ),
            )
            bucket.runs += 1
            if row.error:
                bucket.errors += 1
                continue
            bucket.successes += 1
            bucket.total_tokens += row.total_tokens
            bucket.total_cost_usd += row.cost_usd
            bucket.latency_sum_ms += row.latency_ms

    aggregates: list[ModelAggregate] = []
    for bucket in buckets.values():
        successes = bucket.successes
        avg_latency = bucket.latency_sum_ms / successes if successes > 0 else 0.0
        aggregates.append(
            ModelAggregate(
                model_key=bucket.model_key,
                display_name=bucket.display_name,
                runs=bucket.runs,
                successes=successes,
                errors=bucket.errors,
                total_tokens=bucket.total_tokens,
                total_cost_usd=bucket.total_cost_usd,
                avg_latency_ms=avg_latency,
            )
        )
    return sorted(
        aggregates,
        key=lambda item: (item.errors, -item.successes, item.total_cost_usd),
    )


def _row_status(error: str) -> str:
    return "error" if error else "ok"


def _render_table(tasks: list[str], results: list[ModelAbCompareResult]) -> None:
    click.echo("MULTI-RUN AB MODEL COMPARISON")
    click.echo(f"runs={len(results)}")
    click.echo("")

    for index, (task, result) in enumerate(zip(tasks, results, strict=True), start=1):
        click.echo(f"RUN {index}: {task}")
        click.echo(f"correlation_id={result.correlation_id} status={result.status}")
        header = f"{'model':<24} {'status':<7} {'tokens':>8} {'cost':>10} {'time':>8}"
        click.echo(header)
        click.echo("-" * len(header))
        for row in result.comparison:
            tokens = f"{row.total_tokens:,}" if not row.error else "-"
            cost = f"${row.cost_usd:.6f}" if not row.error else "error"
            latency = f"{row.latency_ms}ms" if not row.error else "-"
            click.echo(
                f"{row.display_name:<24} {_row_status(row.error):<7} "
                f"{tokens:>8} {cost:>10} {latency:>8}"
            )
        if result.models_skipped:
            click.echo(f"skipped={', '.join(result.models_skipped)}")
        click.echo("")

    click.echo("AGGREGATE BY MODEL")
    header = (
        f"{'model':<24} {'ok/runs':>8} {'errors':>6} "
        f"{'tokens':>8} {'cost':>10} {'avg_time':>10}"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for item in _aggregate(results):
        click.echo(
            f"{item.display_name:<24} {item.successes:>2}/{item.runs:<5} "
            f"{item.errors:>6} {item.total_tokens:>8,} "
            f"${item.total_cost_usd:>9.6f} {item.avg_latency_ms:>9.0f}ms"
        )


def _render_json(tasks: list[str], results: list[ModelAbCompareResult]) -> None:
    payload = {
        "tasks": tasks,
        "results": [result.model_dump(mode="json") for result in results],
        "aggregate": [item.__dict__ for item in _aggregate(results)],
    }
    click.echo(json.dumps(payload, indent=2))


def _write_output(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@click.command()
@click.option(
    "--task",
    "tasks",
    multiple=True,
    help="Task prompt to compare. Can be provided multiple times.",
)
@click.option(
    "--tasks-file",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="Optional newline-delimited task prompt file.",
)
@click.option(
    "--models",
    default="all",
    show_default=True,
    help="Comma-separated model IDs, or 'all'.",
)
@click.option("--system-prompt", default=None, help="Optional system prompt override.")
@click.option(
    "--quality-check",
    is_flag=True,
    default=False,
    help="Run ruff on code output.",
)
@click.option(
    "--transport",
    type=click.Choice(["direct", "orchestrator"]),
    default="direct",
    show_default=True,
    help=(
        "Use direct OpenAI-compatible registry calls for demos, or the production "
        "orchestrator/effect chain."
    ),
)
@click.option(
    "--max-tokens",
    type=click.IntRange(min=1),
    default=128,
    show_default=True,
    help="Maximum completion tokens per model call.",
)
@click.option(
    "--include-glm/--no-include-glm",
    default=True,
    show_default=True,
    help="Include configured z.ai GLM as the cloud baseline row.",
)
@click.option(
    "--output",
    type=click.Choice(["table", "json"]),
    default="table",
    show_default=True,
)
@click.option(
    "--output-file",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write rendered output to a file instead of stdout.",
)
def main(
    tasks: tuple[str, ...],
    tasks_file: Path | None,
    models: str,
    system_prompt: str | None,
    quality_check: bool,
    transport: str,
    max_tokens: int,
    include_glm: bool,
    output: str,
    output_file: Path | None,
) -> None:
    """Run several AB comparisons and aggregate model results."""

    task_list = _load_tasks(tasks, tasks_file)
    model_list = _split_models(models)
    results = _run_suite(
        tasks=task_list,
        models=model_list,
        system_prompt=system_prompt,
        quality_check=quality_check,
        include_glm=include_glm,
        transport=transport,
        max_tokens=max_tokens,
    )

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        if output == "json":
            _render_json(task_list, results)
        else:
            _render_table(task_list, results)
    rendered = buffer.getvalue()

    if output_file is not None:
        _write_output(output_file, rendered)
    else:
        click.echo(rendered, nl=False)

    sys.exit(0)


__all__ = ["main"]
