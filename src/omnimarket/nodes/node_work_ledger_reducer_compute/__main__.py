# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_work_ledger_reducer_compute`` (OMN-25629): the window replay's command form.

Reads the ledger file and its archive rolls, optionally the serve log and the recorded append requests,
runs the node's ``window_replay`` operation and prints ``differences=<n>`` with the window and the first
differences. Exit 0 when the difference list is empty, 1 when it is not, 2 when an input is unreadable.
``--plant-guard-break TYPE`` is the positive control: a guard that refuses every row of TYPE, so the same run
shows a non-empty list.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

import click

from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_judge import (
    judge_row,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_shadow_judge import (
    JudgeFn,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_window_replay import (
    WorkLedgerWindowReplay,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.logic.work_ledger_window_sources import (
    accepted_rows,
    join_refusals,
    recorded_requests,
    serve_log_refusals,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
)
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_window_replay import (
    ModelRefusedRequest,
    ModelWindowReplayRequest,
)

PLANTED_RULE = "OMN-25629 planted guard break"


def planted_judge(row_type: str) -> JudgeFn:
    marker = f" | {row_type} | "

    def judge(row: str, existing: str, ledger_name: str) -> ModelJudgeVerdict:
        if marker in row:
            return ModelJudgeVerdict(
                verdict=EnumJudgeVerdict.REFUSE,
                rule=PLANTED_RULE,
                reason=PLANTED_RULE,
            )
        return judge_row(row, existing, ledger_name)

    return judge


def _archive_texts(archive_dir: Path | None) -> list[str]:
    """Texts of the archive rolls, oldest name first; click opens each file (the framework's input boundary)."""
    if archive_dir is None:
        return []
    texts: list[str] = []
    for roll in sorted(archive_dir.glob("*.md")):
        with click.open_file(str(roll), encoding="utf-8") as handle:
            texts.append(handle.read())
    return texts


@click.command()
@click.option("--ledger", type=click.File("r", encoding="utf-8"), required=True)
@click.option(
    "--archive-dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option("--days", type=click.IntRange(min=1), default=14, show_default=True)
@click.option("--serve-log", type=click.File("r", encoding="utf-8"))
@click.option("--requests", "requests_file", type=click.File("r", encoding="utf-8"))
@click.option("--plant-guard-break", "planted_type", default=None)
@click.option("--show", type=click.IntRange(min=0), default=20, show_default=True)
def main(
    ledger: TextIO,
    archive_dir: Path | None,
    days: int,
    serve_log: TextIO | None,
    requests_file: TextIO | None,
    planted_type: str | None,
    show: int,
) -> None:
    if (serve_log is None) != (requests_file is None):
        click.echo("--serve-log and --requests go together", err=True)
        sys.exit(2)
    rows = accepted_rows([*_archive_texts(archive_dir), ledger.read()])
    refused: tuple[ModelRefusedRequest, ...] = ()
    unreadable: tuple[str, ...] = ()
    if serve_log is not None and requests_file is not None:
        refused, unreadable = join_refusals(
            serve_log_refusals(serve_log.read()),
            recorded_requests(requests_file.read()),
        )
    handler = (
        WorkLedgerWindowReplay(judge=planted_judge(planted_type))
        if planted_type
        else WorkLedgerWindowReplay()
    )
    result = handler.handle(
        ModelWindowReplayRequest(
            ledger_name=Path(ledger.name).name,
            accepted_rows=rows,
            window_days=days,
            refused_requests=refused,
            unreadable_refusals=unreadable,
        )
    )
    click.echo(
        f"differences={result.difference_count} rows_judged={result.rows_judged} "
        f"refused_judged={result.refused_judged} window={result.window_start}..{result.window_end}"
    )
    for difference in (*result.row_differences, *result.refusal_differences)[:show]:
        click.echo(f"  {difference.row_ref}: {difference.difference}")
    for request_id in result.unreadable_refusals[:show]:
        click.echo(f"  {request_id}: refused in the serve log, request not recorded")
    sys.exit(0 if result.equivalent else 1)


if __name__ == "__main__":
    main()
