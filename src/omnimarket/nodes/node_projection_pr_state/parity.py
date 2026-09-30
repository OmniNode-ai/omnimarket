# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compare open PRs in schema-1 watcher state with the projection (OMN-19999).

Run with --state-file and either --projection-json (export {"rows": [...]}) or
--dsn-env naming an environment variable containing the database DSN. Exit 0
means nonempty exact parity, 1 means mismatch/empty, 2 means input or DB error.
Only compared fields enter reports; PR bodies are neither retained nor printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_export import (
    ModelPrStateParityExport,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_mismatch import (
    ModelPrStateParityMismatch,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_report import (
    ModelPrStateParityReport,
)
from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_row import (
    ModelPrStateParityRow,
)
from omnimarket.nodes.node_projection_pr_state.models.model_watcher_state import (
    ModelWatcherState,
)

_SELECT = """
    SELECT repo, pr_number, state, head_sha, draft, armed, labels, watcher_class, ci_verdict
    FROM omninode_internal.pr_state WHERE state = 'open'
"""


def watcher_rows(state: ModelWatcherState) -> tuple[ModelPrStateParityRow, ...]:
    rows = []
    for key, pr in state.prs.items():
        f = pr.facts
        if key != f"{f.repo}#{f.number}":
            raise ValueError("watcher PR key disagrees with facts")
        if f.state != "OPEN":
            continue
        rows.append(
            ModelPrStateParityRow(
                repo=f.repo,
                pr_number=f.number,
                state="open",
                head_sha=f.head_sha,
                draft=f.draft,
                armed=f.armed,
                labels=f.labels,
                watcher_class=pr.watcher_class,
                ci_verdict=pr.ci.verdict if pr.ci else "NONE",
            )
        )
    return tuple(rows)


def _open_index(
    rows: tuple[ModelPrStateParityRow, ...],
) -> dict[str, ModelPrStateParityRow]:
    seen: set[str] = set()
    result = {}
    for row in rows:
        if row.key in seen:
            raise ValueError("duplicate PR key in projection export")
        seen.add(row.key)
        if row.state == "open":
            result[row.key] = row
    return result


def compare(
    *,
    file_rows: tuple[ModelPrStateParityRow, ...],
    projection_rows: tuple[ModelPrStateParityRow, ...],
) -> ModelPrStateParityReport:
    """Pure comparison restricted to the two open sets."""
    expected, actual = _open_index(file_rows), _open_index(projection_rows)
    mismatches: list[ModelPrStateParityMismatch] = []
    for key in sorted(expected.keys() - actual.keys()):
        mismatches.append(
            ModelPrStateParityMismatch(kind="in-file-not-in-projection", key=key)
        )
    for key in sorted(actual.keys() - expected.keys()):
        mismatches.append(
            ModelPrStateParityMismatch(kind="in-projection-open-not-in-file", key=key)
        )
    for key in sorted(expected.keys() & actual.keys()):
        want, have = expected[key], actual[key]
        fields: tuple[
            tuple[str, str | bool | tuple[str, ...], str | bool | tuple[str, ...]], ...
        ] = (
            ("head_sha", want.head_sha, have.head_sha),
            ("draft", want.draft, have.draft),
            ("armed", want.armed, have.armed),
            ("labels", tuple(sorted(want.labels)), tuple(sorted(have.labels))),
            ("watcher_class", want.watcher_class, have.watcher_class),
            ("ci_verdict", want.ci_verdict, have.ci_verdict),
        )
        for field, left, right in fields:
            if left != right:
                mismatches.append(
                    ModelPrStateParityMismatch(
                        kind="field-mismatch",
                        key=key,
                        field=field,
                        expected=left,
                        actual=right,
                    )
                )
    return ModelPrStateParityReport(
        file_open_prs=len(expected),
        projection_open_prs=len(actual),
        mismatches=tuple(mismatches),
    )


async def _load_projection_db(dsn: str) -> tuple[ModelPrStateParityRow, ...]:
    import asyncpg

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        rows = []
        for record in await conn.fetch(_SELECT):
            row = dict(record)
            # asyncpg's default JSONB codec returns text, unlike a JSON export.
            if isinstance(row["labels"], str):
                row["labels"] = json.loads(row["labels"])
            rows.append(ModelPrStateParityRow.model_validate_json(json.dumps(row)))
        return tuple(rows)
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--projection-json", type=Path)
    source.add_argument(
        "--dsn-env", help="Name of the environment variable holding the database DSN"
    )
    args = parser.parse_args(argv)
    try:
        state = ModelWatcherState.model_validate_json(
            args.state_file.read_text(encoding="utf-8")
        )
        if args.projection_json is not None:
            projection = ModelPrStateParityExport.model_validate_json(
                args.projection_json.read_text(encoding="utf-8")
            ).rows
        else:
            dsn = os.environ[args.dsn_env]
            if not dsn.strip():
                raise ValueError("empty database DSN")
            projection = asyncio.run(_load_projection_db(dsn))
        report = compare(file_rows=watcher_rows(state), projection_rows=projection)
    except ValidationError as exc:
        # Do not print Pydantic input_value: it may contain a whole watcher PR.
        sys.stderr.write(
            f"pr_state_parity: invalid input ({exc.error_count()} validation errors)\n"
        )
        return 2
    except Exception as exc:  # CLI boundary includes asyncpg connection/query errors.
        # Exception text can include a DSN or input document; keep it out of reports.
        sys.stderr.write(f"pr_state_parity: error ({type(exc).__name__})\n")
        return 2
    sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    return 0 if report.exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
