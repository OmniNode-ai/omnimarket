# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Committed lab command for a graded capability run with recorded identities.

The labelled items predate the record of the serving backend, so the map folds
none of them. ``grid-run`` is the graded run of plan 13.10 that records who
served each answer: it answers each distinct stored prompt
(``delegation_eval_items``) on each backend named in a backends file by calling
that backend's endpoint, grades the answer with the class rubric, and puts on
every verdict row the backend it asked, the host that endpoint lives on and the
backend and model that answered. The run goes to the rule-7a writer of this
node, the same writer the runtime dispatches in process.

It runs offline, in batches, never on a live request. ``--rows-out`` keeps the
content-free verdict rows (no prompt, no answer) as JSON lines,
``--grades-cache`` lets a stopped run resume without asking a backend again,
``--run-out`` keeps the map run the writer folds, and the receipt printed on
stdout is content free. ``--items-file`` takes the stored prompts exported from
``delegation_eval_items`` in place of the database read.

DSNs come from environment variables named on the command line, with no default
(root rule 8): ``--read-dsn-env`` reads the items, ``--write-dsn-env`` is the
tenant projection writer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import threading
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    load_task_class_authority,
)
from omnimarket.lab_work.named_env import read_named_env
from omnimarket.models.delegation.model_size_band import ModelSizeBand
from omnimarket.models.delegation_capability_map.model_capability_configuration import (
    ModelCapabilityConfiguration,
)
from omnimarket.models.delegation_capability_map.model_capability_grid import (
    ModelCapabilityGridGrade,
    ModelCapabilityGridItem,
    ModelCapabilityGridModel,
    ModelCapabilityGridRequest,
)
from omnimarket.models.delegation_capability_map.model_capability_map_run_recorded import (
    ModelCapabilityMapRunRecorded,
)
from omnimarket.models.delegation_capability_map.model_capability_serving_assignment import (
    ModelCapabilityServingAssignment,
)
from omnimarket.models.delegation_capability_map.model_capability_verdict_row import (
    ModelCapabilityVerdictRow,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_capability_grid_run import (
    HandlerCapabilityGridRun,
)
from omnimarket.nodes.node_projection_delegation_capability_map.handlers.handler_delegation_capability_map_writer import (
    HandlerDelegationCapabilityMapWriter,
)
from omnimarket.nodes.node_projection_delegation_capability_map.lab_run import _band

GRID_HARNESS_REVISION = "capability_grid_run"
GRID_GRADER_REVISION = "rubric_check"
#: The three fields that name the machine that served a graded answer.
SERVING_FIELDS: tuple[str, ...] = ("backend_id", "host", "answering_backend")
_RUBRIC_FAIL = "FAIL"
_THINK_END = "</think>"

_GRID_ITEMS_SQL = """
    SELECT i.item_key, i.task_class, i.prompt_snapshot
    FROM public.delegation_eval_items AS i
    WHERE i.tenant_id = $1::uuid AND i.rubric_version = $2
"""

ProtocolGridPost = Callable[[str, dict[str, Any], float], dict[str, Any]]


class ModelGridBackend(BaseModel):
    """One backend a graded run asks: its key, endpoint and the model it serves."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    served_model_id: str = Field(min_length=1)

    def grid_model(self) -> ModelCapabilityGridModel:
        return ModelCapabilityGridModel(
            model_key=self.model_key,
            backend_id=self.backend_id,
            model_revision=self.served_model_id,
        )


class ModelGridAnswer(BaseModel):
    """One endpoint's answer and the machine that served it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    answer_text: str
    host: str | None
    answering_backend: str | None
    served_model: str | None
    finish_reason: str | None = None


def row_identity_gaps(row: ModelCapabilityVerdictRow) -> tuple[str, ...]:
    """The serving fields this verdict row leaves empty, in declared order."""
    return tuple(name for name in SERVING_FIELDS if not getattr(row, name))


def _http_post(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        loaded: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return loaded


def _answer_text(response: Mapping[str, Any]) -> str:
    choices = response.get("choices") or [{}]
    message = choices[0].get("message") or {}
    text = str(message.get("content") or "")
    if _THINK_END in text:
        text = text.rsplit(_THINK_END, 1)[1]
    return text.strip()


def call_grid_backend(
    backend: ModelGridBackend,
    prompt: str,
    *,
    timeout: float,
    post: ProtocolGridPost = _http_post,
) -> ModelGridAnswer:
    """Ask one backend's endpoint and record the host, backend and model that answered.

    The host is the machine the called endpoint lives on, the answering backend
    is the backend whose endpoint returned the answer, and the served model is
    the model the endpoint reported. Transport errors propagate.
    """
    response = post(
        backend.endpoint_url,
        {
            "model": backend.served_model_id,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout,
    )
    served = str(response.get("model") or "") or None
    choices = response.get("choices") or [{}]
    return ModelGridAnswer(
        answer_text=_answer_text(response),
        host=urlparse(backend.endpoint_url).hostname or None,
        answering_backend=backend.backend_id,
        served_model=served,
        finish_reason=str(choices[0].get("finish_reason") or "") or None,
    )


def grid_items(
    raw: Sequence[Mapping[str, Any]],
    authority: ModelTaskClassAuthority | None = None,
) -> tuple[list[ModelCapabilityGridItem], dict[str, int]]:
    """One banded grid item per distinct stored prompt, and what was skipped.

    A prompt stored under several item keys is one item, kept under its least
    item key, so a duplicate never counts twice toward a cell.
    """
    classes = authority or load_task_class_authority()
    skipped: Counter[str] = Counter()
    by_prompt: dict[str, Mapping[str, Any]] = {}
    for item in sorted(raw, key=lambda r: str(r["item_key"])):
        prompt = str(item.get("prompt_snapshot") or "")
        if prompt in by_prompt:
            skipped["duplicate_prompt"] += 1
            continue
        by_prompt[prompt] = item
    items: list[ModelCapabilityGridItem] = []
    for prompt, item in by_prompt.items():
        if not prompt.strip():
            skipped["empty_prompt"] += 1
            continue
        band = _band(classes, str(item.get("task_class") or "unknown"), prompt)
        if not isinstance(band, ModelSizeBand):
            skipped[f"size_band_{band}"] += 1
            continue
        items.append(
            ModelCapabilityGridItem(
                item_key=str(item["item_key"]),
                task_class=band.task_class,
                size_band=band.band,
                prompt=prompt,
            )
        )
    return items, dict(sorted(skipped.items()))


def build_grid_map_run(
    *,
    tenant_id: UUID,
    items: Sequence[ModelCapabilityGridItem],
    models: Sequence[ModelCapabilityGridModel],
    configuration: ModelCapabilityConfiguration,
    grades: Mapping[tuple[str, str], ModelCapabilityGridGrade],
    suite_revision: str,
    grader_revision: str,
    observed_at: datetime,
) -> tuple[ModelCapabilityMapRunRecorded, dict[str, int]]:
    """One map run from the graded items of each model, and its counts.

    An item a backend never answered (a transport error) has no grade and makes
    no row for that backend.
    """
    rows: list[ModelCapabilityVerdictRow] = []
    assignments: list[ModelCapabilityServingAssignment] = []
    for model in models:
        graded = [i for i in items if (i.item_key, model.backend_id) in grades]
        request_kwargs: dict[str, Any] = {
            "tenant_id": tenant_id,
            "models": (model,),
            "configurations": (configuration,),
            "suite_revision": suite_revision,
            "grader_revision": grader_revision,
            "harness_revision": GRID_HARNESS_REVISION,
        }
        identity = ModelCapabilityGridRequest.model_construct(
            **request_kwargs, items=()
        ).identity_for(model, configuration)
        assignments.append(
            ModelCapabilityServingAssignment(
                model_key=model.model_key,
                backend_id=model.backend_id,
                configuration_id=configuration.configuration_id,
                identity=identity,
            )
        )
        if not graded:
            continue
        request = ModelCapabilityGridRequest(**request_kwargs, items=tuple(graded))

        def grade(
            item: ModelCapabilityGridItem,
            model: ModelCapabilityGridModel,
            configuration: ModelCapabilityConfiguration,
        ) -> ModelCapabilityGridGrade:
            del configuration
            return grades[(item.item_key, model.backend_id)]

        rows.extend(HandlerCapabilityGridRun(grade).handle(request))
    digest = hashlib.sha256(
        json.dumps(
            sorted(
                (r.item_key, r.model_key, r.backend_id, r.task_class, r.passed)
                for r in rows
            )
        ).encode("utf-8")
    ).hexdigest()
    run = ModelCapabilityMapRunRecorded(
        tenant_id=tenant_id,
        map_run_id=str(uuid5(NAMESPACE_URL, f"omn-21434:grid:{digest}")),
        observed_at=observed_at,
        rows=tuple(rows),
        assignments=tuple(assignments),
    )
    return run, {
        "verdict_rows": len(rows),
        "rows_missing_serving_field": sum(1 for r in rows if row_identity_gaps(r)),
    }


def _gate_accepts(task_class: str, prompt: str, answer: ModelGridAnswer) -> bool:
    """Whether the live acceptance gate accepts the answer.

    The deterministic quality gate with the task class's DoD checks and response
    contract, then the class's required bar, as the bus-less delegation port
    decides acceptance (without its independent answer judge).
    """
    from omnibase_core.models.delegation.wire import ModelQualityGateInput

    from omnimarket.inference.provider_finish_reason import parse_finish_reason
    from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
        RequiredBarAuthorityError,
        is_quality_accepted,
        resolve_required_bar_authority,
    )
    from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
        delta as evaluate_quality_gate,
    )
    from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
        resolve_task_class_dod_checks,
        resolve_task_class_response_contract,
    )

    deterministic, heuristic = resolve_task_class_dod_checks(task_class, prompt=prompt)
    result = evaluate_quality_gate(
        ModelQualityGateInput(
            correlation_id=uuid5(
                NAMESPACE_URL, f"omn-21434:gate:{task_class}:{prompt}"
            ),
            task_type=task_class,
            llm_response_content=answer.answer_text,
            dod_deterministic=deterministic,
            dod_heuristic=heuristic,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
        ),
        response_contract=resolve_task_class_response_contract(task_class),
        grounding_source=prompt,
        finish_reason=parse_finish_reason(answer.finish_reason),
    )
    try:
        authority = resolve_required_bar_authority(task_type=task_class)
    except RequiredBarAuthorityError:
        return result.fail_category != "fail_deterministic" and result.passed
    declared, _ = resolve_task_class_dod_checks(task_class)
    return is_quality_accepted(result, authority, declared_deterministic=declared)


def _rubric_grade(task_class: str, prompt: str, answer: ModelGridAnswer) -> bool:
    """A pass is an answer the acceptance gate accepts and the class rubric does not fail.

    The class rubric is UNDETERMINED where it has no rubric for the class or
    cannot check a criterion without the task's sources, so it decides only
    where it can; the acceptance gate decides every class.
    """
    from omnimarket.delegation.rubric.attempt_verdict import (
        record_attempt_rubric_verdict,
    )

    verdict = record_attempt_rubric_verdict(
        task_class=task_class, request_text=prompt, answer_text=answer.answer_text
    )
    if verdict.outcome == _RUBRIC_FAIL:
        return False
    return _gate_accepts(task_class, prompt, answer)


def _load_cache(path: Path | None) -> dict[tuple[str, str], ModelCapabilityGridGrade]:
    cached: dict[tuple[str, str], ModelCapabilityGridGrade] = {}
    if path is None or not path.exists():
        return cached
    for line in path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            cached[(record["item_key"], record["backend_id"])] = (
                ModelCapabilityGridGrade.model_validate(record["grade"])
            )
    return cached


def grade_grid(
    items: Sequence[ModelCapabilityGridItem],
    backends: Sequence[ModelGridBackend],
    *,
    timeout: float,
    concurrency: int,
    cache_path: Path | None = None,
    post: ProtocolGridPost | None = None,
) -> tuple[dict[tuple[str, str], ModelCapabilityGridGrade], dict[str, int]]:
    """Ask every backend every item once, grade each answer with the class rubric.

    A call that times out is a failed answer from the backend that was asked;
    any other transport error makes no grade and is counted.
    """
    send = post or _http_post
    grades = _load_cache(cache_path)
    counts: Counter[str] = Counter(cached=len(grades))
    lock = threading.Lock()
    todo = [
        (item, backend)
        for backend in backends
        for item in items
        if (item.item_key, backend.backend_id) not in grades
    ]

    def one(pair: tuple[ModelCapabilityGridItem, ModelGridBackend]) -> None:
        item, backend = pair
        try:
            answer = call_grid_backend(backend, item.prompt, timeout=timeout, post=send)
            grade = ModelCapabilityGridGrade(
                passed=_rubric_grade(item.task_class, item.prompt, answer),
                host=answer.host,
                answering_backend=answer.answering_backend,
                served_model=answer.served_model,
            )
            outcome = "answered"
        except TimeoutError:
            grade = ModelCapabilityGridGrade(
                passed=False,
                host=urlparse(backend.endpoint_url).hostname or None,
                answering_backend=backend.backend_id,
                served_model=backend.served_model_id,
            )
            outcome = "timed_out"
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            with lock:
                counts["call_errors"] += 1
            return
        with lock:
            grades[(item.item_key, backend.backend_id)] = grade
            counts[outcome] += 1
            if cache_path is not None:
                with cache_path.open("a") as handle:
                    handle.write(
                        json.dumps(
                            {
                                "item_key": item.item_key,
                                "backend_id": backend.backend_id,
                                "grade": grade.model_dump(mode="json"),
                            }
                        )
                        + "\n"
                    )

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        list(pool.map(one, todo))
    return grades, dict(sorted(counts.items()))


async def _read_grid_items(
    args: argparse.Namespace, tenant: str
) -> list[dict[str, Any]]:
    reader = AsyncpgAdapter(dsn=read_named_env(args.read_dsn_env))
    await reader.connect()
    try:
        return await reader.execute(
            _GRID_ITEMS_SQL, tenant, args.rubric_version, tenant=tenant
        )
    finally:
        await reader.close()


def _grid_run(args: argparse.Namespace) -> dict[str, Any]:
    from omnimarket.delegation.rubric.contract_loader import (
        load_delegation_class_rubrics,
    )

    tenant = UUID(args.tenant_id)
    backends = [
        ModelGridBackend.model_validate(entry)
        for entry in json.loads(Path(args.backends_file).read_text())
    ]
    if args.items_file:
        raw = json.loads(Path(args.items_file).read_text())
    else:
        if not (args.read_dsn_env and args.rubric_version):
            raise SystemExit(
                "grid-run reads delegation_eval_items with --read-dsn-env and "
                "--rubric-version, or takes --items-file"
            )
        raw = asyncio.run(_read_grid_items(args, str(tenant)))
    items, skipped = grid_items(raw)
    if args.limit_per_cell:
        kept: Counter[tuple[str, str]] = Counter()
        limited = []
        for item in items:
            key = (item.task_class, item.size_band.value)
            if kept[key] < args.limit_per_cell:
                kept[key] += 1
                limited.append(item)
        items = limited
    grades, calls = grade_grid(
        items,
        backends,
        timeout=args.timeout_seconds,
        concurrency=args.concurrency,
        cache_path=Path(args.grades_cache) if args.grades_cache else None,
    )
    configuration = ModelCapabilityConfiguration()
    run, counts = build_grid_map_run(
        tenant_id=tenant,
        items=items,
        models=[b.grid_model() for b in backends],
        configuration=configuration,
        grades=grades,
        suite_revision=load_delegation_class_rubrics().rubric_version,
        grader_revision=GRID_GRADER_REVISION,
        observed_at=datetime.now(tz=UTC),
    )
    if args.rows_out:
        with Path(args.rows_out).open("w") as handle:
            for row in run.rows:
                handle.write(row.model_dump_json() + "\n")
    cells: dict[str, list[int]] = {}
    for row in run.rows:
        tally = cells.setdefault(
            f"{row.backend_id}|{row.task_class}|{row.size_band.value}", [0, 0]
        )
        tally[0] += 1
        tally[1] += int(row.passed)
    receipt: dict[str, Any] = {
        "map_run_id": run.map_run_id,
        "prompts": len(items),
        "items_skipped": skipped,
        "calls": calls,
        "configuration_id": configuration.configuration_id,
        "rows_by_backend_class_band": {
            k: {"n": v[0], "passes": v[1]} for k, v in sorted(cells.items())
        },
        **counts,
    }
    if args.run_out:
        Path(args.run_out).write_text(run.model_dump_json() + "\n")
    if args.no_write:
        return receipt
    if not args.write_dsn_env:
        raise SystemExit(
            "grid-run writes the map run with --write-dsn-env, or --no-write"
        )
    writer = HandlerDelegationCapabilityMapWriter()
    writer.bind_projection_database_url(read_named_env(args.write_dsn_env))
    receipt["writer"] = writer.handle(run.model_dump(mode="json"))
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="delegation-capability-grid-run")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--read-dsn-env")
    parser.add_argument("--write-dsn-env")
    sub = parser.add_subparsers(dest="command", required=True)
    grid = sub.add_parser("grid-run")
    grid.add_argument("--rubric-version")
    grid.add_argument(
        "--items-file",
        help="JSON list of {item_key, task_class, prompt_snapshot} in place of the DB read",
    )
    grid.add_argument(
        "--run-out", help="write the content-free map run (the writer's input) as JSON"
    )
    grid.add_argument("--backends-file", required=True)
    grid.add_argument("--timeout-seconds", type=float, default=240.0)
    grid.add_argument("--concurrency", type=int, default=4)
    grid.add_argument("--limit-per-cell", type=int, default=0)
    grid.add_argument("--grades-cache")
    grid.add_argument("--rows-out")
    grid.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    receipt = _grid_run(args)
    json.dump(receipt, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
