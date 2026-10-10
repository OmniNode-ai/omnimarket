# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B fanout effect. Delegation runs on the existing bus chain, once per item.

The responder chain already escalates within one call. Retrying here hides its failed
terminal. The process exit code is never the terminal; only receipt facts decide it.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_request import (
    ModelFanoutItem,
    ModelFanoutRequest,
)
from omnimarket.nodes.node_delegate_fanout_effect.models.model_fanout_result import (
    ModelFanoutResult,
    ModelFanoutRow,
)

READY_CONTROL_PROMPT = "Reply with exactly the word READY"
DEFAULT_TIMEOUT_S = 300
HARD_BOUND_MARGIN_S = 60
EXCERPT_CHARS = 400
IMPERATIVE_VERBS = frozenset(
    [
        "add",
        "analyse",
        "analyze",
        "answer",
        "assess",
        "assert",
        "build",
        "calculate",
        "categorise",
        "categorize",
        "check",
        "choose",
        "classify",
        "clean",
        "combine",
        "compare",
        "compute",
        "condense",
        "contrast",
        "convert",
        "correct",
        "count",
        "create",
        "critique",
        "decide",
        "defend",
        "delete",
        "derive",
        "describe",
        "design",
        "diagnose",
        "disprove",
        "draft",
        "draw",
        "enumerate",
        "estimate",
        "evaluate",
        "expand",
        "explain",
        "extract",
        "find",
        "fix",
        "format",
        "generate",
        "give",
        "group",
        "identify",
        "improve",
        "infer",
        "judge",
        "label",
        "list",
        "map",
        "measure",
        "merge",
        "name",
        "normalise",
        "normalize",
        "note",
        "outline",
        "plan",
        "polish",
        "predict",
        "produce",
        "propose",
        "prove",
        "rank",
        "rate",
        "read",
        "recommend",
        "record",
        "refine",
        "reformat",
        "refute",
        "remove",
        "reply",
        "replace",
        "report",
        "resolve",
        "return",
        "review",
        "rewrite",
        "score",
        "select",
        "shorten",
        "show",
        "sketch",
        "solve",
        "sort",
        "split",
        "state",
        "suggest",
        "summarise",
        "summarize",
        "tag",
        "tell",
        "test",
        "trace",
        "translate",
        "turn",
        "validate",
        "verify",
        "walk",
        "write",
    ]
)
TABLE_COLUMNS = (
    "label",
    "correlation_id",
    "run_id",
    "terminal",
    "terminal_failure_cause",
    "quality",
    "model",
    "cost_usd",
    "wall_ms",
    "artifacts",
    "result_excerpt",
)


def admit(item: ModelFanoutItem) -> dict[str, bool | str]:
    label = item.label.strip()
    prompt = item.prompt
    if not label:
        return {
            "ok": False,
            "reason": "item carries no label; a row with no label cannot be reported",
        }
    if not prompt.strip():
        return {"ok": False, "reason": f"item {label} carries an empty prompt"}
    match = re.match(r"^[a-z]+", prompt.strip().lower())
    word = match[0] if match else ""
    if word not in IMPERATIVE_VERBS:
        opener = f'"{word}"' if word else "no word"
        addition = f'"{word}"' if word else "this word"
        return {
            "ok": False,
            "reason": (
                f"item {label}: the prompt opens with {opener}, which is not in the declared "
                "imperative-verb allowlist. A delegated prompt states a task; open it with a verb. "
                f"If {addition} is a legitimate imperative, add it to IMPERATIVE_VERBS -- "
                "do not loosen the check."
            ),
        }
    if (
        re.match(r"^write\s+an?\b", prompt.strip(), re.I)
        and not (item.task_type or "").strip()
    ):
        return {
            "ok": False,
            "reason": (
                f'item {label}: the prompt opens with "write a"/"write an" '
                "and declares no task_type. "
                "With the class unstated it resolves from the contract's selection predicates, and "
                "that opener claims the prompt for code_generation, so prose is then "
                "graded against "
                "a code rubric. State task_type (e.g. document, summarization, planning) "
                "and re-run."
            ),
        }
    if item.test_shaped and not (item.task_type or "").strip():
        return {
            "ok": False,
            "reason": (
                f"item {label}: the item is marked test_shaped and declares no task_type. "
                "The auto-classifier currently routes test-shaped prose to code_generation "
                "and grades "
                "it against a code rubric (four measured failures, 2026-09-21). "
                "State the class you "
                "want -- planning and document both work for prose about tests; "
                "test is for producing "
                "the tests themselves."
            ),
        }
    criteria = item.criteria or []
    if not isinstance(criteria, list):
        return {
            "ok": False,
            "reason": f"item {label}: criteria must be an array of declared slugs",
        }
    for criterion in criteria:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]+", js_string(criterion)):
            encoded = json.dumps(criterion, ensure_ascii=False, separators=(",", ":"))
            return {
                "ok": False,
                "reason": (
                    f"item {label}: criterion {encoded} is not a declared slug. "
                    "--criteria is repeatable "
                    "and takes one slug per occurrence; comma-joined free text is a CLI "
                    "usage error "
                    "that produces no correlation id."
                ),
            }
    timeout = item.timeout_s
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout < 1
        or int(timeout) != timeout
    ):
        return {
            "ok": False,
            "reason": f"item {label}: timeout_s must be a positive integer number of seconds",
        }
    return {"ok": True}


def js_string(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text or "item").strip("-")[:60] or "item"


def hard_bound_seconds(item: ModelFanoutItem) -> int:
    value = item.timeout_s
    base = (
        int(value)
        if isinstance(value, (int, float)) and value > 0
        else DEFAULT_TIMEOUT_S
    )
    return base + HARD_BOUND_MARGIN_S


def excerpt(text: str) -> str:
    unwrapped = text
    at = text.find("ONEX_FANOUT_ROW")
    brace = text.find("{", at) if at >= 0 else -1
    if brace >= 0:
        try:
            parsed = json.loads(text[brace:])
            if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
                unwrapped = parsed["response"]
        except ValueError:
            pass
    compact = re.sub(r"\s+", " ", unwrapped).strip()
    return (
        compact if len(compact) <= EXCERPT_CHARS else compact[: EXCERPT_CHARS - 1] + "…"
    )


def refusal_row(item: ModelFanoutItem, reason: str, lane: str) -> ModelFanoutRow:
    return ModelFanoutRow(
        label=item.label or "(unlabelled)",
        lane=lane,
        terminal="REFUSED_PRECHECK",
        terminal_failure_cause=reason,
    )


def dispatched_row(
    item: ModelFanoutItem, lane: str, read: dict[str, JsonValue]
) -> ModelFanoutRow:
    facts = {
        key: value for key, value in read.items() if key in ModelFanoutRow.model_fields
    }
    facts.update(
        label=item.label or "(unlabelled)",
        lane=lane,
        terminal=read.get("terminal") or "NO_RECEIPT",
        result_excerpt=excerpt(str(read.get("response") or "")),
    )
    return ModelFanoutRow.model_validate(facts)


def is_failed(row: ModelFanoutRow) -> bool:
    return row.terminal.lower() != "completed"


def closeout_fragment(rows: Sequence[ModelFanoutRow]) -> str:
    ids = [r.correlation_id for r in rows if r.correlation_id]
    return (
        f"delegate_receipts={len(ids)} correlation_ids={','.join(ids)} "
        f"failed={sum(is_failed(r) for r in rows)}"
    )


def markdown_table(rows: Sequence[ModelFanoutRow]) -> str:
    def cell(value: object) -> str:
        return (
            "--"
            if value is None
            else js_string(value).replace("|", r"\|").replace("\n", " ")
        )

    header = "| " + " | ".join(TABLE_COLUMNS) + " |"
    rule = "| " + " | ".join("---" for _ in TABLE_COLUMNS) + " |"
    body = [
        "| " + " | ".join(cell(getattr(row, key)) for key in TABLE_COLUMNS) + " |"
        for row in rows
    ]
    return "\n".join([header, rule, *body])


def _object(value: JsonValue) -> dict[str, JsonValue]:
    return value if isinstance(value, dict) else {}


def read_receipt(root: Path) -> dict[str, JsonValue]:
    """The old reader's exact precedence: receipt > hard kill > stdout > stderr."""

    def tail(name: str) -> str:
        path = root / name
        lines = path.read_text(errors="replace").splitlines() if path.is_file() else []
        return next((line.rstrip() for line in reversed(lines) if line.strip()), "")

    marker = root / "hard_timeout.txt"
    killed = marker.read_text(errors="replace").strip() if marker.is_file() else ""
    runs = root / "runs"
    receipts = sorted(runs.glob("*/receipt.json")) if runs.is_dir() else []
    if not receipts:
        return {
            "terminal": "NO_RECEIPT",
            "correlation_id": None,
            "run_id": None,
            "terminal_failure_cause": (
                "hard timeout: the wrapper did not return within the bound"
                if killed
                else "no run directory was created"
            ),
            "quality": None,
            "model": None,
            "cost_usd": None,
            "wall_ms": None,
            "artifacts": None,
            "response": "",
            "last_stdout_line": killed
            or tail("stdout.txt")
            or tail("stderr.txt")
            or "(no output on stdout or stderr)",
        }
    path = receipts[0]
    receipt = cast("dict[str, JsonValue]", json.loads(path.read_text()))
    inner = _object(receipt.get("receipt"))
    payload = _object(
        _object(_object(inner.get("result")).get("terminal_payload")).get("payload")
    )
    metrics = _object(payload.get("metrics"))
    cause = str(
        payload.get("terminal_failure_cause")
        or receipt.get("terminal_failure_cause")
        or ""
    )
    if killed:
        cause += " | " + killed
    return {
        "terminal": payload.get("status") or receipt.get("status"),
        "correlation_id": receipt.get("correlation_id")
        or payload.get("correlation_id"),
        "run_id": receipt.get("run_id") or path.parent.name,
        "terminal_failure_cause": cause or None,
        "quality": payload.get("quality_score"),
        "model": payload.get("model_name") or receipt.get("model"),
        "cost_usd": metrics.get("cost_usd")
        if metrics.get("cost_usd") is not None
        else receipt.get("cost_usd"),
        "wall_ms": inner.get("duration_ms"),
        "artifacts": str(root / "artifacts"),
        "response": payload.get("response") or "",
        "last_stdout_line": None,
    }


def delegate_argv(
    item: ModelFanoutItem, root: Path, lane: str, omni_home: Path
) -> list[str]:
    """Publish via the sanctioned delegation client, with argv rather than shell interpolation."""
    argv = [
        "bash",
        str(omni_home / "omnibase_infra/scripts/onex"),
        "delegate",
        item.prompt,
        "--bus",
        "kafka",
        "--locus",
        "deployed-lane",
        "--lane",
        lane,
        "--omnibase-path",
        str(omni_home),
        "--state-root",
        str(root),
    ]
    if item.task_type:
        argv.extend(["--task-type", item.task_type])
    if isinstance(item.criteria, list):
        for criterion in item.criteria:
            argv.extend(["--criteria", js_string(criterion)])
    if item.timeout_s:
        argv.extend(["--timeout", str(int(cast("int", item.timeout_s)))])
    if item.response_contract:
        path = root / "contract.json"
        value = item.response_contract
        path.write_text(
            value if isinstance(value, str) else json.dumps(value, indent=2)
        )
        argv.extend(["--response-contract", str(path)])
    return argv


def dispatch_item(item: ModelFanoutItem, root: Path, lane: str) -> dict[str, JsonValue]:
    omni_home = os.environ.get("OMNI_HOME", "")
    if not omni_home:
        raise RuntimeError(
            "OMNI_HOME is unset; cannot resolve the sanctioned delegation wrapper"
        )
    root.mkdir(parents=True)
    (root / "prompt.txt").write_text(item.prompt)
    argv = delegate_argv(item, root, lane, Path(omni_home))
    bound = hard_bound_seconds(item)
    with (
        (root / "stdout.txt").open("w") as stdout,
        (root / "stderr.txt").open("w") as stderr,
    ):
        process = subprocess.Popen(
            argv, stdout=stdout, stderr=stderr, start_new_session=True
        )
        try:
            process.wait(timeout=bound)
        except subprocess.TimeoutExpired:
            # The group is this invocation's new session, never a shared runtime or peer.
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)
            (root / "hard_timeout.txt").write_text(
                f"hard timeout after {bound}s: wrapper did not return"
            )
    return read_receipt(root)


class HandlerDelegateFanout:
    """Stateless typed effect; each admitted item is sent exactly once."""

    def __init__(
        self,
        delegate: Callable[
            [ModelFanoutItem, Path, str], dict[str, JsonValue]
        ] = dispatch_item,
    ) -> None:
        self._delegate = delegate

    def handle(self, request: ModelFanoutRequest) -> ModelFanoutResult:
        admitted: list[ModelFanoutItem] = []
        rows: list[ModelFanoutRow] = []
        for item in request.items:
            verdict = admit(item)
            if verdict["ok"]:
                admitted.append(item)
            else:
                rows.append(refusal_row(item, str(verdict["reason"]), request.lane))
        if admitted:
            parent = request.state_root
            if parent is None:
                omni_home = os.environ.get("OMNI_HOME", "")
                if not omni_home:
                    raise RuntimeError(
                        "OMNI_HOME is unset; provide it before dispatching"
                    )
                parent = Path(omni_home) / ".claude_scratch" / "delegation-fanout"
            parent.mkdir(parents=True, exist_ok=True)
            run_root = Path(
                tempfile.mkdtemp(prefix=slugify(request.run_slug) + "-", dir=parent)
            )
            # A barrier per chunk preserves the old queue discipline and result ordering.
            with ThreadPoolExecutor(max_workers=request.max_parallel) as pool:
                for base in range(0, len(admitted), request.max_parallel):
                    group = admitted[base : base + request.max_parallel]
                    futures = [
                        pool.submit(
                            self._delegate,
                            item,
                            run_root / f"{base + n:02d}-{slugify(item.label)}",
                            request.lane,
                        )
                        for n, item in enumerate(group)
                    ]
                    for item, future in zip(group, futures, strict=True):
                        rows.append(dispatched_row(item, request.lane, future.result()))
        ids = [row.correlation_id for row in rows if row.correlation_id]
        return ModelFanoutResult(
            lane=request.lane,
            items=len(request.items),
            admitted=len(admitted),
            refused=len(request.items) - len(admitted),
            rows=rows,
            receipts=len(ids),
            failed=sum(is_failed(row) for row in rows),
            correlation_ids=ids,
            closeout_fragment=closeout_fragment(rows),
            markdown_table=markdown_table(rows),
        )
