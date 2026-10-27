# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Row-for-row acceptance proof must reject fabricated or incomplete answers."""

from uuid import uuid4

import pytest

from omnimarket.models.work_ledger_query import ModelWorkLedgerQueryRequest
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_history_verification import (
    verify,
)
from tests.test_work_ledger_history_query import CUTOFF, SOURCES, Reader, record

pytestmark = pytest.mark.unit


@pytest.fixture
def proof(tmp_path):
    rows = tuple(record(i) for i in range(1, 5))
    archives = {}
    for index, source in enumerate(SOURCES):
        path = tmp_path / f"roll-{index}.md"
        path.write_text(
            "\n".join(row.raw_row for row in rows[index * 2 : index * 2 + 2]) + "\n"
        )
        archives[source] = path
    request = ModelWorkLedgerQueryRequest.model_validate(
        {
            "correlation_id": str(uuid4()),
            "query": "rows",
            "until": CUTOFF,
            "history": {
                "ledger_id": "rolling-work-ledger",
                "first_seq": 1,
                "last_seq": 4,
                "sources": SOURCES,
            },
        }
    )
    handler = HandlerWorkLedgerQuery(reader=Reader(rows))
    return (
        request,
        archives,
        handler.handle(request),
        handler.handle(request.model_copy(update={"limit": 2})),
    )


def test_archive_acceptance_compares_every_row_and_truncated_answer(proof):
    request, archives, full, truncated = proof
    result = verify(request, archives, full, truncated)
    assert result["rows"] == 4
    assert result["sources"] == 2
    assert result["truncated_rows"] == 2


@pytest.mark.parametrize(
    "defect",
    [
        "raw",
        "source",
        "sequence",
        "time",
        "cutoff",
        "missing",
        "order",
        "error",
        "verdict",
        "snapshot",
        "prefix",
        "cursor",
        "range",
        "filter",
    ],
)
def test_archive_acceptance_fails_closed(proof, defect):
    request, archives, full, truncated = proof
    rows = list(full.rows)
    changes = {
        "raw": {"raw_row": rows[0].raw_row + " changed"},
        "source": {"source": "live"},
        "sequence": {"ledger_seq": 9},
        "time": {"ingested_at": None},
        "cutoff": {"ingested_at": CUTOFF},
    }
    if defect in changes:
        rows[0] = rows[0].model_copy(update=changes[defect])
    elif defect == "missing":
        rows.pop()
    elif defect == "order":
        rows.reverse()
    elif defect == "error":
        full = full.model_copy(update={"error": "read failed"})
    elif defect == "verdict":
        truncated = truncated.model_copy(
            update={
                "completeness": truncated.completeness.model_copy(
                    update={"status": "complete"}
                )
            }
        )
    elif defect == "snapshot":
        truncated = truncated.model_copy(
            update={
                "completeness": truncated.completeness.model_copy(
                    update={"source_hash": "0" * 64}
                )
            }
        )
    elif defect == "prefix":
        truncated = truncated.model_copy(
            update={"rows": tuple(reversed(truncated.rows))}
        )
    elif defect == "cursor":
        truncated = truncated.model_copy(update={"next_cursor": None})
    elif defect == "range":
        request = request.model_copy(
            update={"history": request.history.model_copy(update={"last_seq": 5})}
        )
    elif defect == "filter":
        request = request.model_copy(update={"lanes": ("missing",)})
    full = full.model_copy(update={"rows": tuple(rows)})
    with pytest.raises(ValueError, match=r".+"):
        verify(request, archives, full, truncated)


def test_archive_acceptance_cli_reports_counts_without_ledger_text(
    proof, tmp_path, monkeypatch, capsys
):
    from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_history_verification import (
        main,
    )

    request, archives, full, truncated = proof
    inputs = {"request": request, "response": full, "truncated-response": truncated}
    argv = ["verify-history"]
    for name, model in inputs.items():
        path = tmp_path / f"{name}.json"
        path.write_text(model.model_dump_json())
        argv.extend([f"--{name}", str(path)])
    for source, path in archives.items():
        argv.extend(["--archive", f"{source}={path}"])
    monkeypatch.setattr("sys.argv", argv)
    assert main() == 0
    output = capsys.readouterr().out
    assert '"rows": 4' in output
    assert full.rows[0].raw_row not in output
    # Pydantic validation errors contain their input; the CLI must redact them.
    (tmp_path / "response.json").write_text('{"private-ledger-text": "keep-private"}')
    assert main() == 1
    output = capsys.readouterr()
    assert "verification failed" in output.err
    assert "keep-private" not in output.err
