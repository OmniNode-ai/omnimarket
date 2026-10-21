# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state context source: open work and orchestration state (OMN-21111)."""

from __future__ import annotations

import importlib
import inspect
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.models.context.model_context_access import ModelContextAccess
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from omnibase_core.models.context.model_context_source_request import (
    ModelContextSourceRequest,
)
from omnibase_core.models.context.model_context_source_result import (
    ModelContextSourceResult,
)

from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    LedgerSourceReadError,
    MarkdownLedgerRowReader,
    ModelLedgerSourceRow,
    ProtocolLedgerRowReader,
)
from omnimarket.nodes.node_context_source_work_state_effect import (
    HandlerContextSourceWorkState,
    NodeContextSourceWorkStateEffect,
)
from omnimarket.nodes.node_context_source_work_state_effect.handlers.handler_context_source_work_state import (
    ModelWorkStateBinding,
)

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[4]
NODE = "node_context_source_work_state_effect"
NODE_DIR = REPO / "src" / "omnimarket" / "nodes" / NODE
TENANT = "lab"
WATERMARK = "2026-10-19T08:00:00Z"
OPEN_WORK = EnumContextFactor.OPEN_WORK
STATE = EnumContextFactor.ORCHESTRATION_STATE

HANDOFF = "2026-10-19T07:00:00Z | MSG | from=orch-a | id=2026-10-19T07:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | handoff: finish the metering producer and read path"
ANSWERED = "2026-10-19T07:05:00Z | MSG | from=orch-a | id=2026-10-19T07:05:00Z-orch-a | to=orch-a | ticket=OMN-1 | handoff: already answered"
ACK = "2026-10-19T07:06:00Z | ACK | lane=orch-a | re=2026-10-19T07:05:00Z-orch-a | ticket=OMN-1 | done"
STATUS_REPORT = "2026-10-19T07:10:00Z | MSG | from=child-1 | id=2026-10-19T07:10:00Z-child-1 | to=orch-a | ticket=OMN-2 | status report from a child"
OTHER_LANE = "2026-10-19T07:11:00Z | MSG | from=orch-b | id=2026-10-19T07:11:00Z-orch-b | to=orch-b | ticket=OMN-3 | not this session"
STALE_HANDOFF = "2026-10-17T07:00:00Z | MSG | from=orch-a | id=2026-10-17T07:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | two days old"
LATE_HANDOFF = "2026-10-19T09:00:00Z | MSG | from=orch-a | id=2026-10-19T09:00:00Z-orch-a | to=orch-a | ticket=OMN-1 | after the watermark"
OPEN_CHILD = "2026-10-19T07:20:00Z | CLAIM | lane=child-1 | actor=codex | ticket=OMN-2 | parent=sess-p | worktree=none | est ~1 lane-hours; displaces x; (OMN-2) | open child"
CLOSED_CHILD = "2026-10-19T07:21:00Z | CLAIM | lane=child-2 | actor=codex | ticket=OMN-4 | parent=sess-p | worktree=none | est ~1 lane-hours; displaces x; (OMN-4) | closed child"
CHILD_DONE = "2026-10-19T07:30:00Z | TERMINAL | lane=child-2 | outcome=done | friction=none | delegated=0 delegation_reason=no-text-or-code | done"
OWN_CLAIM = "2026-10-19T06:00:00Z | CLAIM | lane=orch-a | actor=claude | ticket=OMN-1 | worktree=none | est ~8 lane-hours; displaces x; (OMN-1) | session"
UNRELATED_CLAIM = "2026-10-19T07:22:00Z | CLAIM | lane=child-9 | actor=codex | ticket=OMN-9 | parent=sess-z | worktree=none | est ~1 lane-hours; displaces x; (OMN-9) | other parent"

ROWS = (
    STALE_HANDOFF,
    OWN_CLAIM,
    HANDOFF,
    ANSWERED,
    ACK,
    STATUS_REPORT,
    OTHER_LANE,
    OPEN_CHILD,
    CLOSED_CHILD,
    UNRELATED_CLAIM,
    CHILD_DONE,
    LATE_HANDOFF,
)


def _ledger(tmp_path: Path, rows: tuple[str, ...] = ROWS) -> MarkdownLedgerRowReader:
    path = tmp_path / "ROLLING_WORK_LEDGER.md"
    path.write_text("# ledger\n\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return MarkdownLedgerRowReader([path])


def _handler(
    reader: ProtocolLedgerRowReader, *, senders: str = "self"
) -> HandlerContextSourceWorkState:
    return HandlerContextSourceWorkState(
        reader=reader,
        tenant_ids=(TENANT,),
        work_state=ModelWorkStateBinding.model_validate(
            {"horizon_hours": 24, "open_work_senders": senders}
        ),
    )


def _request(
    factor: EnumContextFactor,
    *,
    lane: str | None = "orch-a",
    scopes: tuple[str, ...] = ("session:sess-p",),
    revision: str = WATERMARK,
    tenant: str = TENANT,
) -> ModelContextSourceRequest:
    return ModelContextSourceRequest(
        factor=factor,
        scopes=scopes,
        caller_scope=ModelContextCallerScope(lane=lane),
        access=ModelContextAccess(tenant_id=tenant),
        revision=revision,
    )


def _texts(result: ModelContextSourceResult) -> list[str]:
    return [entry.text for entry in result.entries]


def test_open_work_is_every_unanswered_message_to_the_lane(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(_request(OPEN_WORK))
    assert result.status is EnumContextSourceStatus.RESOLVED
    assert _texts(result) == [HANDOFF]
    entry = result.entries[0]
    assert entry.verbatim_verified is True
    assert entry.span is not None
    assert entry.span.end == len(HANDOFF)
    assert entry.source_ref == "ROLLING_WORK_LEDGER.md:5"
    assert result.entry_counts == {"work_ledger": 1, "stale": 1}


def test_open_work_with_any_sender_keeps_reports_addressed_to_the_lane(
    tmp_path: Path,
) -> None:
    result = _handler(_ledger(tmp_path), senders="any").handle(_request(OPEN_WORK))
    assert _texts(result) == [HANDOFF, STATUS_REPORT]


def test_orchestration_state_is_every_open_child_claim(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(_request(STATE))
    assert result.status is EnumContextSourceStatus.RESOLVED
    # The session's own claim (lane) and its open child (parent); the closed child
    # and the other parent's claim are not orchestration state of this session.
    assert _texts(result) == [OWN_CLAIM, OPEN_CHILD]


def test_a_row_after_the_watermark_is_not_read(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(
        _request(OPEN_WORK, revision="2026-10-19T06:59:59Z")
    )
    assert result.status is EnumContextSourceStatus.UNRESOLVED
    assert result.entries == ()


def test_an_unknown_tenant_is_denied(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(_request(OPEN_WORK, tenant="other"))
    assert result.status is EnumContextSourceStatus.DENIED


def test_a_revision_that_is_not_a_row_stamp_is_an_invalid_request(
    tmp_path: Path,
) -> None:
    result = _handler(_ledger(tmp_path)).handle(_request(OPEN_WORK, revision="head"))
    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.error_code is EnumContextSourceErrorCode.INVALID_REQUEST


def test_a_request_naming_no_lane_is_an_invalid_request(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(_request(STATE, lane=None, scopes=()))
    assert result.error_code is EnumContextSourceErrorCode.INVALID_REQUEST


def test_a_factor_it_does_not_serve_is_unresolved(tmp_path: Path) -> None:
    result = _handler(_ledger(tmp_path)).handle(
        _request(EnumContextFactor.OPERATOR_RULINGS)
    )
    assert result.status is EnumContextSourceStatus.UNRESOLVED


class _BrokenReader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        raise LedgerSourceReadError("OperationalError: connection refused")


def test_a_failed_read_is_unavailable_never_an_empty_resolved() -> None:
    result = _handler(_BrokenReader()).handle(_request(OPEN_WORK))
    assert result.status is EnumContextSourceStatus.UNAVAILABLE
    assert result.entries == ()


def test_the_contract_declares_its_topics_and_handler_signature() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    command = "onex.cmd.omnimarket.context-source-work-state-requested.v1"
    event = "onex.evt.omnimarket.context-source-work-state-completed.v1"
    assert contract["event_bus"]["subscribe_topics"] == [command]
    assert contract["event_bus"]["publish_topics"] == [event]
    assert contract["terminal_event"] == event
    assert contract["context_source_work_state"] == {
        "horizon_hours": 24,
        "open_work_senders": "self",
    }
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert cls is HandlerContextSourceWorkState
    assert issubclass(NodeContextSourceWorkStateEffect, cls)
    hints = inspect.get_annotations(cls.handle, eval_str=True)
    assert hints == {
        "request": ModelContextSourceRequest,
        "return": ModelContextSourceResult,
    }
    assert cls.synchronous_execution is True
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["entry-points"]["onex.nodes"][NODE] == (
        f"omnimarket.nodes.{NODE}"
    )


def test_the_handler_hardcodes_no_topic_string() -> None:
    for path in (NODE_DIR / "handlers").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "onex.cmd." not in text, path
        assert "onex.evt." not in text, path
