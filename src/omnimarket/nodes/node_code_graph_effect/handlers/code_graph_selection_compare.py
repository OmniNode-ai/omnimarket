# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""compare_selection: the graph's REACH answer beside the parse selection of a merged pull request.

The lookup runs in a worker the caller terminates at ``lookup_deadline_seconds``; a Memgraph that does
not answer in time falls to the file store, which has its own ``fallback_deadline_seconds`` so the
first cannot consume it. The most a comparison takes is the sum of the two plus the worker teardown.
Rows are appended through the configured appender and nothing gates.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from omnimarket.models.model_code_graph_lookup import (
    EnumCodeGraphQuestion,
    EnumLookupFallbackReason,
    EnumLookupServedFrom,
    ModelCodeGraphLookupCommand,
    ModelCodeGraphLookupResult,
)
from omnimarket.models.model_code_graph_selection_compare import (
    EnumCompareOutcome,
    EnumGraphAnswerSource,
    ModelLookupDeadlineExceeded,
    ModelSelectionCompareCommand,
    ModelSelectionCompareResult,
    ModelSelectionCompareRow,
)
from omnimarket.nodes.node_code_graph_effect.adapters.memgraph_version_store import (
    MemgraphVersionStore,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_deadline_worker import (
    WorkerOutcome,
    run_with_deadline,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_lookup import (
    CodeGraphLookup,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolReadingAppender,
)

#: The ledger row type and source cell of a comparison row.
ROW_TYPE = "STATUS"
ROW_SOURCE_CELL = "src=code-graph-selection-compare"
#: Seconds on top of the two deadlines the whole comparison may take (worker start and teardown).
TEARDOWN_ALLOWANCE_SECONDS = 1.0


def memgraph_reach(
    open_store: Callable[[], MemgraphVersionStore],
    command: ModelCodeGraphLookupCommand,
) -> ModelCodeGraphLookupResult:
    """The REACH answer from Memgraph alone. A transport fault raises; the caller owns the fallback."""
    store = open_store()
    try:
        result = CodeGraphLookup(store, store).handle(command)
    finally:
        store.close()
    return result.model_copy(update={"served_from": EnumLookupServedFrom.MEMGRAPH})


def _covered(selected: tuple[str, ...], test_file: str) -> bool:
    return any(
        test_file == path or (path.endswith("/") and test_file.startswith(path))
        for path in selected
    )


class SelectionCompare:
    """Compare the graph's reach with the parse selection of one merged pull request."""

    def __init__(
        self,
        *,
        memgraph_lookup: Callable[
            [ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult
        ],
        file_lookup: Callable[
            [ModelCodeGraphLookupCommand], ModelCodeGraphLookupResult
        ],
        is_test: Callable[[str], bool],
        lookup_deadline_seconds: float,
        fallback_deadline_seconds: float,
        appender: ProtocolReadingAppender | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if lookup_deadline_seconds <= 0 or fallback_deadline_seconds <= 0:
            raise ValueError("both deadlines are above zero")
        self._memgraph = memgraph_lookup
        self._files = file_lookup
        self._is_test = is_test
        self._lookup_deadline = lookup_deadline_seconds
        self._fallback_deadline = fallback_deadline_seconds
        self._appender = appender
        self._clock = clock

    def handle(
        self, command: ModelSelectionCompareCommand
    ) -> ModelSelectionCompareResult:
        row = self.compare(command)
        if self._appender is None:
            return ModelSelectionCompareResult(row=row, ledger="not-written")
        appended = self._appender.append(
            render_row(row) + "\n",
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL, f"selection-compare|{command.correlation_id}"
                )
            ),
        )
        if not appended.ok:
            return ModelSelectionCompareResult(
                row=row,
                ledger="append-failed",
                failure=f"append-failed:{appended.status}:{reason_token(appended.message or 'no-message')}",
            )
        return ModelSelectionCompareResult(
            row=row,
            ledger="written" if appended.status == "accepted" else "already-written",
        )

    def compare(
        self, command: ModelSelectionCompareCommand
    ) -> ModelSelectionCompareRow:
        started = time.monotonic()
        lookup = ModelCodeGraphLookupCommand(
            correlation_id=command.correlation_id,
            question=EnumCodeGraphQuestion.REACH,
            repo=command.repo,
            paths=command.changed_files,
        )
        deadlines: list[ModelLookupDeadlineExceeded] = []
        fallback_reason: EnumLookupFallbackReason | None = None
        answer: ModelCodeGraphLookupResult | None = None
        failure: str | None = None

        first = run_with_deadline(lambda: self._memgraph(lookup), self._lookup_deadline)
        if first.status == "ok" and first.value is not None:
            answer = first.value
        elif first.status == "deadline":
            deadlines.append(
                ModelLookupDeadlineExceeded(
                    stage="memgraph",
                    deadline_seconds=self._lookup_deadline,
                    elapsed_seconds=first.elapsed_seconds,
                )
            )
            fallback_reason = EnumLookupFallbackReason.READ_TIMED_OUT
        elif first.error_reason is not None:
            fallback_reason = EnumLookupFallbackReason(first.error_reason)
        else:
            failure = f"memgraph refused: {first.error_type}: {first.error_text}"

        if answer is None and failure is None:
            second = run_with_deadline(
                lambda: self._files(lookup), self._fallback_deadline
            )
            if second.status == "ok" and second.value is not None:
                answer = second.value.model_copy(
                    update={
                        "served_from": EnumLookupServedFrom.FILE_STORE,
                        "fallback_reason": fallback_reason,
                    }
                )
            elif second.status == "deadline":
                deadlines.append(
                    ModelLookupDeadlineExceeded(
                        stage="file-store",
                        deadline_seconds=self._fallback_deadline,
                        elapsed_seconds=second.elapsed_seconds,
                    )
                )
                failure = "the file store did not answer inside its deadline"
            else:
                failure = _raised(second)

        return self._row(
            command,
            answer=answer,
            failure=failure,
            fallback_reason=fallback_reason,
            deadlines=tuple(deadlines),
            elapsed=time.monotonic() - started,
        )

    def _row(
        self,
        command: ModelSelectionCompareCommand,
        *,
        answer: ModelCodeGraphLookupResult | None,
        failure: str | None,
        fallback_reason: EnumLookupFallbackReason | None,
        deadlines: tuple[ModelLookupDeadlineExceeded, ...],
        elapsed: float,
    ) -> ModelSelectionCompareRow:
        compared_at = self._clock()

        def make(
            outcome: EnumCompareOutcome,
            source: EnumGraphAnswerSource,
            *,
            indexed: str | None = None,
            reason: str | None = None,
            graph_only: tuple[str, ...] = (),
            parse_only: int = 0,
            both: int = 0,
        ) -> ModelSelectionCompareRow:
            return ModelSelectionCompareRow(
                correlation_id=command.correlation_id,
                repo=command.repo,
                pr_number=command.pr_number,
                base_sha=command.base_sha,
                head_sha=command.head_sha,
                compared_at=compared_at,
                outcome=outcome,
                compared_stage=command.parse.stage,
                selector_version=command.parse.selector_version,
                policy_version=command.parse.policy_version,
                graph_source=source,
                indexed_commit=indexed,
                fallback_reason=fallback_reason,
                graph_only=len(graph_only),
                parse_only=parse_only,
                both=both,
                graph_only_paths=graph_only,
                deadlines=deadlines,
                elapsed_seconds=elapsed,
                reason=reason,
            )

        if answer is None or answer.status == "failed" or answer.answered_from is None:
            return make(
                EnumCompareOutcome.LOOKUP_FAILED,
                EnumGraphAnswerSource.NONE if answer is None else _source(answer),
                reason=failure
                or (answer.reason if answer is not None else None)
                or "no store answered",
            )
        source = _source(answer)
        if command.historical:
            return make(
                EnumCompareOutcome.HISTORICAL_UNVERSIONED,
                source,
                indexed=answer.answered_from,
                reason="a merge from before the selection record: the graph cannot answer for its base",
            )
        if answer.answered_from != command.base_sha:
            return make(
                EnumCompareOutcome.SNAPSHOT_MISMATCH,
                source,
                indexed=answer.answered_from,
                reason=f"indexed at {answer.answered_from}, the pull request's base is {command.base_sha}",
            )
        graph = {
            match.path for match in answer.matches if self._is_test_file(match.path)
        } | {path for path in command.changed_files if self._is_test_file(path)}
        parse = {
            path
            for path in command.test_files
            if self._is_test_file(path) and _covered(command.parse.selected_paths, path)
        }
        return make(
            EnumCompareOutcome.COMPARED,
            source,
            indexed=answer.answered_from,
            graph_only=tuple(sorted(graph - parse)),
            parse_only=len(parse - graph),
            both=len(graph & parse),
        )

    def _is_test_file(self, path: str) -> bool:
        return path.endswith(".py") and self._is_test(path)


def _source(answer: ModelCodeGraphLookupResult) -> EnumGraphAnswerSource:
    return (
        EnumGraphAnswerSource.MEMGRAPH
        if answer.served_from is EnumLookupServedFrom.MEMGRAPH
        else EnumGraphAnswerSource.FILE_STORE
    )


def _raised(outcome: WorkerOutcome[ModelCodeGraphLookupResult]) -> str:
    return f"the file store raised {outcome.error_type}: {outcome.error_text}"


def _cell(text: str) -> str:
    return " ".join(text.replace("|", "/").split()).replace(" ", "_")


def render_row(row: ModelSelectionCompareRow) -> str:
    """The ledger row of a comparison: one line, one field per cell."""
    cells = [
        "lane=code-graph",
        ROW_SOURCE_CELL,
        "model=none",
        f"repo={row.repo}",
        f"pr={row.pr_number if row.pr_number is not None else 'none'}",
        f"base={row.base_sha}",
        f"head={row.head_sha}",
        f"outcome={row.outcome.value}",
        f"stage={row.compared_stage}",
        f"selector={row.selector_version}",
        f"policy={row.policy_version}",
        f"graph_source={row.graph_source.value}",
        f"indexed={row.indexed_commit or 'none'}",
        f"fallback={row.fallback_reason.value if row.fallback_reason else 'none'}",
        f"graph_only={row.graph_only}",
        f"parse_only={row.parse_only}",
        f"both={row.both}",
        f"valid={'yes' if row.valid_for_verdict else 'no'}",
        "graph_only_paths="
        + (",".join(_cell(path) for path in row.graph_only_paths) or "none"),
        "deadlines="
        + (
            ",".join(f"{d.stage}:{d.deadline_seconds:g}" for d in row.deadlines)
            or "none"
        ),
        f"elapsed={row.elapsed_seconds:.2f}",
        f"correlation={_cell(row.correlation_id)}",
        "gate=none",
        f"compared_at={stamp_text(row.compared_at)}",
        "shadow: the graph beside the parse; nothing gates"
        + (f"; {_cell(row.reason)}" if row.reason else ""),
    ]
    return f"{stamp_text(row.compared_at)} | {ROW_TYPE} | " + " | ".join(cells)
