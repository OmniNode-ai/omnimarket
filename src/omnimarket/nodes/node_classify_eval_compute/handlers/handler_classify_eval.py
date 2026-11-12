# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify eval: build the labelled set from a ledger copy, or score engine replies against the labels.

Exemplar: ``nodes/node_code_eval_suite_census_compute/handlers/handler_code_eval_suite_census.py``
(a pure compute that reads its values from its own overlay and ends a bad request as a typed failure).
It differs in that the task kind is a labelled text line, not a merged code ticket, and the node has
two operations behind one request model. No clock, no model call, no I/O but the overlay.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.eval_classify.enum_classify import (
    EnumClassifyEvalFailure,
    EnumClassifyOperation,
    EnumIntegrationClass,
)
from omnimarket.models.eval_classify.model_classify_eval import (
    ModelClassifyAnswer,
    ModelClassifyClassCount,
    ModelClassifyClassScore,
    ModelClassifyConfusionCell,
    ModelClassifyEngineScore,
    ModelClassifyEvalRequest,
    ModelClassifyEvalResult,
)
from omnimarket.models.eval_classify.model_classify_overlay import (
    ModelClassifyEvalOverlay,
)
from omnimarket.models.eval_classify.model_classify_task import ModelClassifyEvalTask

_OVERLAY_ID = "node_classify_eval_compute"
_CELL_SEPARATOR = " | "
_HEADING = re.compile(r"^##\s+(.*)$")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_REFERENCE = re.compile(
    r"^(?P<repository>[A-Za-z0-9_.-]+)\s+(?P<number>\d+|\(no number\))$"
)
# the cells that name the answer: never carried into a task
_ANSWER_CELLS = ("state:", "step:", "owner:")
_RUNS_CELL = "runs:"
_EVIDENCE_CELLS = ("gap:", "reading:", "reading owed:")


class HandlerClassifyEval:
    """One handler, two operations: ``build_set`` and ``score_answers``."""

    def __init__(self, overlay: ModelClassifyEvalOverlay | None = None) -> None:
        self._overlay = overlay

    def handle(self, request: ModelClassifyEvalRequest) -> ModelClassifyEvalResult:
        if request.operation is EnumClassifyOperation.SCORE_ANSWERS:
            return self._score(request)
        return self._build(request)

    # ---- build_set -------------------------------------------------------------------------

    def _build(self, request: ModelClassifyEvalRequest) -> ModelClassifyEvalResult:
        try:
            overlay = self._overlay or load_structured_overlay(
                _OVERLAY_ID, ModelClassifyEvalOverlay
            )
        except NodeOverlayError as error:
            return self._failure(
                request, EnumClassifyEvalFailure.OVERLAY_UNAVAILABLE, str(error)
            )
        by_id: dict[str, list[ModelClassifyEvalTask]] = defaultdict(list)
        for task in self._parse(request.ledger_text or "", overlay):
            by_id[task.task_id].append(task)
        if not by_id:
            return self._failure(
                request,
                EnumClassifyEvalFailure.NO_LEDGER_LINES,
                "no line sits under a section the overlay labels",
            )
        # a line listed under two different classes has no label to measure against
        ambiguous = {
            task_id
            for task_id, rows in by_id.items()
            if len({row.label for row in rows}) > 1
        }
        usable: dict[EnumIntegrationClass, list[ModelClassifyEvalTask]] = defaultdict(
            list
        )
        for task_id, rows in by_id.items():
            if task_id not in ambiguous:
                usable[rows[0].label].append(rows[0])
        drawn: list[ModelClassifyEvalTask] = []
        counts: list[ModelClassifyClassCount] = []
        for label in sorted(overlay.per_class_n, key=lambda item: item.value):
            wanted = overlay.per_class_n[label]
            pool = sorted(
                usable[label], key=lambda item: self._order_key(overlay.seed, item)
            )
            counts.append(
                ModelClassifyClassCount(
                    label=label, available=len(pool), drawn=min(wanted, len(pool))
                )
            )
            if len(pool) < wanted:
                return self._failure(
                    request,
                    EnumClassifyEvalFailure.CLASS_SHORTFALL,
                    f"class {label.value} holds {len(pool)} usable lines, the overlay asks for {wanted}",
                    class_counts=tuple(counts),
                    ambiguous_excluded=len(ambiguous),
                )
            drawn.extend(pool[:wanted])
        if len(drawn) < overlay.min_total:
            return self._failure(
                request,
                EnumClassifyEvalFailure.BELOW_MINIMUM_TOTAL,
                f"{len(drawn)} lines drawn, the overlay's minimum is {overlay.min_total}",
                class_counts=tuple(counts),
                ambiguous_excluded=len(ambiguous),
            )
        masked_cells = 0
        masked: list[ModelClassifyEvalTask] = []
        for task in drawn:
            title = self._mask(task.title, overlay)
            runs = self._mask(task.runs, overlay)
            evidence = self._mask(task.evidence, overlay)
            masked_cells += sum(
                after != before
                for before, after in (
                    (task.title, title),
                    (task.runs, runs),
                    (task.evidence, evidence),
                )
            )
            masked.append(
                task.model_copy(
                    update={"title": title, "runs": runs, "evidence": evidence}
                )
            )
        return ModelClassifyEvalResult(
            suite_id=request.suite_id,
            operation=request.operation,
            rubric=overlay.rubric,
            tasks=tuple(masked),
            class_counts=tuple(counts),
            ambiguous_excluded=len(ambiguous),
            masked_cells=masked_cells,
        )

    @staticmethod
    def _mask(text: str, overlay: ModelClassifyEvalOverlay) -> str:
        for phrase in overlay.leak_phrases:
            text = text.replace(phrase, overlay.leak_mask)
        return text

    @staticmethod
    def _order_key(seed: str, task: ModelClassifyEvalTask) -> str:
        return hashlib.sha256(f"{seed}:{task.task_id}".encode()).hexdigest()

    def _parse(
        self, text: str, overlay: ModelClassifyEvalOverlay
    ) -> list[ModelClassifyEvalTask]:
        label: EnumIntegrationClass | None = None
        tasks: list[ModelClassifyEvalTask] = []
        for line in text.splitlines():
            heading = _HEADING.match(line)
            if heading:
                label = self._section_label(heading.group(1), overlay)
                continue
            if label is None or not line.startswith("- "):
                continue
            task = self._task(line[2:], label)
            if task is not None:
                tasks.append(task)
        return tasks

    @staticmethod
    def _section_label(
        heading: str, overlay: ModelClassifyEvalOverlay
    ) -> EnumIntegrationClass | None:
        for prefix, label in overlay.section_labels.items():
            if heading.startswith(prefix):
                return label
        return None

    @staticmethod
    def _task(body: str, label: EnumIntegrationClass) -> ModelClassifyEvalTask | None:
        cells = [cell.strip() for cell in body.split(_CELL_SEPARATOR)]
        if len(cells) < 3:
            return None
        reference = _REFERENCE.match(cells[0])
        if reference is None:
            return None
        repository, number = reference.group("repository"), reference.group("number")
        title = cells[1]
        merged_at = cells[2].removeprefix("merged ").strip()
        runs = ""
        evidence = ""
        for cell in cells[3:]:
            if cell.startswith(_ANSWER_CELLS):
                continue
            if cell.startswith(_RUNS_CELL):
                runs = cell.removeprefix(_RUNS_CELL).strip()
                continue
            for key in _EVIDENCE_CELLS:
                if cell.startswith(key):
                    evidence = cell.removeprefix(key).strip()
                    break
        if number == "(no number)":
            digest = hashlib.sha256(title.encode()).hexdigest()[:10]
            task_id = f"{repository}:{digest}"
        else:
            task_id = f"{repository}#{number}"
        return ModelClassifyEvalTask(
            task_id=task_id,
            label=label,
            repository=repository,
            title=title,
            merged_at=merged_at,
            runs=runs,
            evidence=evidence,
        )

    # ---- score_answers ---------------------------------------------------------------------

    def _score(self, request: ModelClassifyEvalRequest) -> ModelClassifyEvalResult:
        if not request.tasks:
            return self._failure(
                request,
                EnumClassifyEvalFailure.NO_TASKS,
                "the request carries no tasks",
            )
        if not request.answers:
            return self._failure(
                request,
                EnumClassifyEvalFailure.NO_ANSWERS,
                "the request carries no answers",
            )
        labels = {task.task_id: task.label for task in request.tasks}
        if len(labels) != len(request.tasks):
            return self._failure(
                request, EnumClassifyEvalFailure.DUPLICATE_TASK, "a task id is repeated"
            )
        seen: set[tuple[str, str, str]] = set()
        groups: dict[tuple[str, str], list[ModelClassifyAnswer]] = defaultdict(list)
        for answer in request.answers:
            if answer.task_id not in labels:
                return self._failure(
                    request,
                    EnumClassifyEvalFailure.UNKNOWN_TASK,
                    f"answer names task {answer.task_id}, which is not in the tasks",
                )
            key = (answer.engine, answer.input_variant, answer.task_id)
            if key in seen:
                return self._failure(
                    request,
                    EnumClassifyEvalFailure.DUPLICATE_ANSWER,
                    f"engine {answer.engine} variant {answer.input_variant} answered {answer.task_id} twice",
                )
            seen.add(key)
            groups[(answer.engine, answer.input_variant)].append(answer)
        scores = tuple(
            self._engine_score(engine, variant, answers, labels)
            for (engine, variant), answers in sorted(groups.items())
        )
        return ModelClassifyEvalResult(
            suite_id=request.suite_id, operation=request.operation, scores=scores
        )

    def _engine_score(
        self,
        engine: str,
        variant: str,
        answers: list[ModelClassifyAnswer],
        labels: dict[str, EnumIntegrationClass],
    ) -> ModelClassifyEngineScore:
        asked: Counter[EnumIntegrationClass] = Counter()
        right: Counter[EnumIntegrationClass] = Counter()
        cells: Counter[tuple[EnumIntegrationClass, EnumIntegrationClass | None]] = (
            Counter()
        )
        unparsed = 0
        for answer in answers:
            truth = labels[answer.task_id]
            asked[truth] += 1
            predicted = parse_class(answer.reply)
            cells[(truth, predicted)] += 1
            if predicted is None:
                unparsed += 1
            elif predicted is truth:
                right[truth] += 1
        by_class = tuple(
            ModelClassifyClassScore(
                label=label,
                n=asked[label],
                correct=right[label],
                accuracy=(right[label] / asked[label]) if asked[label] else None,
            )
            for label in sorted(asked, key=lambda item: item.value)
        )
        n = len(answers)
        correct = sum(right.values())
        return ModelClassifyEngineScore(
            engine=engine,
            input_variant=variant,
            n=n,
            correct=correct,
            unparsed=unparsed,
            accuracy=correct / n,
            mean_seconds=sum(answer.seconds for answer in answers) / n,
            by_class=by_class,
            confusion=tuple(
                ModelClassifyConfusionCell(
                    label=label, predicted=predicted, count=count
                )
                for (label, predicted), count in sorted(
                    cells.items(),
                    key=lambda item: (
                        item[0][0].value,
                        item[0][1].value if item[0][1] else "",
                    ),
                )
            ),
        )

    @staticmethod
    def _failure(
        request: ModelClassifyEvalRequest,
        failure: EnumClassifyEvalFailure,
        detail: str,
        class_counts: tuple[ModelClassifyClassCount, ...] = (),
        ambiguous_excluded: int = 0,
    ) -> ModelClassifyEvalResult:
        return ModelClassifyEvalResult(
            suite_id=request.suite_id,
            operation=request.operation,
            failure=failure,
            detail=detail,
            class_counts=class_counts,
            ambiguous_excluded=ambiguous_excluded,
        )


def parse_class(reply: str) -> EnumIntegrationClass | None:
    """The class an engine's reply ends on: its last class word, or None when it names none."""
    normalised = _NON_WORD.sub("_", reply.lower()).strip("_")
    best: tuple[int, EnumIntegrationClass] | None = None
    for label in EnumIntegrationClass:
        position = normalised.rfind(label.value)
        if position >= 0 and (best is None or position > best[0]):
            best = (position, label)
    return best[1] if best else None
