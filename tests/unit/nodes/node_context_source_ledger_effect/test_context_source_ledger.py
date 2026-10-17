# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-ledger context source (OMN-21127, plan task TC.4).

Text parity: every RULING and OPERATOR-CONSENT row of a UTC day comes back with
text equal to its markdown row, through the live reader shape and the pinned
markdown reader; a reader that truncates is caught. Correction closure: the
real D1 ruling carries its CORRECTION whole in ``corrected_by``; a typed
``operator_span`` yields exactly that span as operator words.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_source_error_code import (
    EnumContextSourceErrorCode,
)
from omnibase_core.enums.enum_context_source_status import EnumContextSourceStatus
from omnibase_core.enums.enum_context_supersession_status import (
    EnumContextSupersessionStatus,
)
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
from pydantic import ValidationError

from omnimarket.nodes.node_context_source_ledger_effect import (
    HandlerContextSourceLedger,
    NodeContextSourceLedgerEffect,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.handler_context_source_ledger import (
    quoted_words,
)
from omnimarket.nodes.node_context_source_ledger_effect.handlers.ledger_row_reader import (
    LedgerSourceReadError,
    MarkdownLedgerRowReader,
    ModelLedgerSourceRow,
    ProtocolLedgerRowReader,
)
from omnimarket.nodes.node_context_source_ledger_effect.models import (
    CORRECTED_LABEL,
    FRAMING_LABEL,
    ModelLedgerSourceEntry,
    ModelLedgerSourceResult,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    row_id_of,
)

pytestmark = pytest.mark.unit

TENANT = "lab"
LONG_WORDS = (
    "keep every word of this ruling " * 30
)  # well past the 500-char display cut
DAY = (
    '2026-10-01T00:00:05Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | "use the lab" | first answer',
    "2026-10-01T01:00:00Z | CLAIM | lane=alpha | ticket=OMN-1 | est ~1 lane-hours; displaces x; (OMN-1) | work",
    f'2026-10-01T02:00:00Z | RULING | lane=orch | ticket=OMN-2 | kind=process | "{LONG_WORDS.strip()}" | long row',
    '2026-10-01T03:00:00Z | OPERATOR-CONSENT | lane=orch | "yes" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | evidence',
    '2026-10-01T04:00:00Z | RULING | lane=orch | ticket=OMN-1 | kind=decision | supersedes=2026-10-01T00:00:05Z | "use the pool" | replaces it',
    "2026-10-01T05:00:00Z | TERMINAL | lane=alpha | ticket=OMN-1 | friction=none | done",
    '2026-10-01T23:59:59Z | OPERATOR-CONSENT | lane=orch | extends=2026-10-01T03:00:00Z | "keep going" | APPROVED SCOPE: lab | OUT OF SCOPE: prod | late',
)
NEXT_DAY = '2026-10-02T00:00:00Z | RULING | lane=orch | ticket=OMN-3 | kind=decision | "tomorrow" | after the day'

# The real D1 pair, quoted whole from the rolling ledger, pinned by the sha256
# the plan's excerpt records for each row line.
D1_RULING = '2026-10-02T11:25:17Z | RULING | lane=claude-mods-plan | ticket=OMN-18531 | kind=decision | verbatim=encoded | "D1 (mod file shape / ratchet exception), plan beta/plans/2026-10-01-claude-code-context-and-delegation-adapter-plan.md, decision D1. Operator answer, 2026-10-02, in-session: %22mod file for what? nothing here should be non-canonical%22"'
D1_CORRECTION = "2026-10-02T11:30:10Z | CORRECTION | lane=claude-mods-plan | ticket=OMN-18531 | corrects=2026-10-02T11:25:17Z | RULING row 2026-10-02T11:25:17Z (decision D1) quotes caller framing inside its quoted cell; the operator's words are only the span after \"in-session:\", that is: mod file for what? nothing here should be non-canonical. The framing text before it is context, not the operator's words."
D1_SHA256 = {
    D1_RULING: "479f00b9724b466704de70a3fca18c7c64b87da006479da815633fd3027c546a",
    D1_CORRECTION: "fa71fd2a59b8fe8fa4a0707e08c8d1863417eba6be5fa5be25ab92eaf51dfdd8",
}

SPAN_RULING = '2026-10-03T09:00:00Z | RULING | lane=orch | ticket=OMN-4 | kind=decision | "framing before: use the pool" | recorded'
SPAN_CORRECTION = "2026-10-03T09:05:00Z | CORRECTION | lane=orch | ticket=OMN-4 | corrects=2026-10-03T09:00:00Z | operator_span=16:28 | only the last words are the operator's"


class _ListReader:
    """The live reader's shape: rows as the projection stores them."""

    def __init__(self, raw_rows: tuple[str, ...], *, cut: int | None = None) -> None:
        self._rows = raw_rows
        self._cut = cut

    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        out = []
        for raw in self._rows:
            stamp = datetime.strptime(raw[:20], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=as_of.tzinfo
            )
            if stamp > as_of:
                continue
            text = raw if self._cut is None else raw[: self._cut]
            out.append(
                ModelLedgerSourceRow(
                    row_id=row_id_of(raw),
                    raw_row=text,
                    source_ref=f"work_ledger_rows:{row_id_of(raw)}",
                )
            )
        return tuple(out)


class _BrokenReader:
    def read_rows(self, *, as_of: datetime) -> tuple[ModelLedgerSourceRow, ...]:
        raise LedgerSourceReadError("OperationalError: connection refused")


def _handler(reader: ProtocolLedgerRowReader) -> HandlerContextSourceLedger:
    return HandlerContextSourceLedger(reader=reader, tenant_ids=(TENANT,))


def _request(
    revision: str,
    *,
    cited: tuple[str, ...] = (),
    tenant: str = TENANT,
    factor: EnumContextFactor = EnumContextFactor.OPERATOR_RULINGS,
) -> ModelContextSourceRequest:
    return ModelContextSourceRequest(
        factor=factor,
        caller_scope=ModelContextCallerScope(cited_ruling_stamps=cited),
        access=ModelContextAccess(tenant_id=tenant),
        revision=revision,
    )


def _authority(rows: tuple[str, ...]) -> list[str]:
    return [r for r in rows if r.split(" | ")[1] in {"RULING", "OPERATOR-CONSENT"}]


def _by_stamp(result: ModelLedgerSourceResult) -> dict[str, ModelLedgerSourceEntry]:
    return {entry.row_stamp: entry for entry in result.entries}


def test_entry_text_equals_markdown_row_text_for_every_authority_row_of_a_day(
    tmp_path: Path,
) -> None:
    markdown = tmp_path / "ROLLING_WORK_LEDGER.md"
    markdown.write_text("# ledger\n\n" + "\n".join((*DAY, NEXT_DAY)) + "\n", "utf-8")
    expected = _authority(DAY)
    stamps = tuple(row[:20] for row in expected)
    assert len(expected) == 5
    assert max(len(r) for r in expected) > 900

    for reader in (_ListReader((*DAY, NEXT_DAY)), MarkdownLedgerRowReader([markdown])):
        result = _handler(reader).handle(_request("2026-10-01T23:59:59Z", cited=stamps))
        assert result.status is EnumContextSourceStatus.RESOLVED
        assert sorted(e.text for e in result.entries) == sorted(expected)
        for entry in result.entries:
            assert entry.text_sha256 == hashlib.sha256(entry.text.encode()).hexdigest()
            assert entry.verbatim_verified
            assert entry.span is not None
            assert entry.span.end == len(entry.text)

    # Positive control: a reader that hands back the 500-character display cut
    # is caught by the same comparison.
    cut = _handler(_ListReader(DAY, cut=500)).handle(
        _request("2026-10-01T23:59:59Z", cited=stamps)
    )
    assert sorted(e.text for e in cut.entries) != sorted(expected)


def test_pinned_markdown_reader_keeps_line_refs_across_archive_rolls(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "ROLLING_WORK_LEDGER_2026-10-01-split.md"
    live = tmp_path / "ROLLING_WORK_LEDGER.md"
    archive.write_text("\n".join(DAY[:3]) + "\n", "utf-8")
    live.write_text("\n".join(DAY[3:]) + "\n", "utf-8")
    result = _handler(MarkdownLedgerRowReader([archive, live])).handle(
        _request("2026-10-01T23:59:59Z", cited=(DAY[0][:20], DAY[3][:20]))
    )
    refs = {e.row_stamp: e.source_ref for e in result.entries}
    assert refs == {
        DAY[0][:20]: f"{archive.name}:1",
        DAY[3][:20]: f"{live.name}:1",
    }


def test_in_force_read_drops_superseded_and_honours_the_watermark() -> None:
    result = _handler(_ListReader((*DAY, NEXT_DAY))).handle(
        _request("2026-10-01T23:59:59Z")
    )
    entries = _by_stamp(result)
    assert "2026-10-01T00:00:05Z" not in entries  # superseded at 04:00
    assert NEXT_DAY[:20] not in entries  # after the watermark
    assert (
        entries["2026-10-01T03:00:00Z"].supersession_status
        is EnumContextSupersessionStatus.AMENDED
    )
    assert (
        entries["2026-10-01T04:00:00Z"].supersession_status
        is EnumContextSupersessionStatus.ACTIVE
    )
    cited = _handler(_ListReader(DAY)).handle(
        _request("2026-10-01T23:59:59Z", cited=("2026-10-01T00:00:05Z",))
    )
    (only,) = cited.entries
    assert only.supersession_status is EnumContextSupersessionStatus.SUPERSEDED


def test_d1_ruling_carries_its_correction() -> None:
    for row, digest in D1_SHA256.items():
        assert hashlib.sha256(row.encode("utf-8")).hexdigest() == digest
    for cited in ((), ("2026-10-02T11:25:17Z",)):
        result = _handler(_ListReader((D1_RULING, D1_CORRECTION))).handle(
            _request("2026-10-02T12:00:00Z", cited=cited)
        )
        (entry,) = result.entries
        assert entry.text == D1_RULING
        assert entry.supersession_status is EnumContextSupersessionStatus.CORRECTED
        (correction,) = entry.corrected_by
        assert correction.text == D1_CORRECTION
        assert correction.row_stamp == "2026-10-02T11:30:10Z"
        assert correction.operator_span is None
        assert entry.framing_label == CORRECTED_LABEL
        assert entry.operator_words is None


def test_ruling_before_its_correction_is_read_uncorrected_at_an_earlier_watermark() -> (
    None
):
    result = _handler(_ListReader((D1_RULING, D1_CORRECTION))).handle(
        _request("2026-10-02T11:29:59Z")
    )
    (entry,) = result.entries
    assert entry.corrected_by == ()
    assert entry.supersession_status is EnumContextSupersessionStatus.ACTIVE


def test_a_ruling_cannot_be_built_without_its_corrections() -> None:
    result = _handler(_ListReader((D1_RULING, D1_CORRECTION))).handle(
        _request("2026-10-02T12:00:00Z")
    )
    (entry,) = result.entries
    fields = entry.model_dump()
    with pytest.raises(ValidationError):
        ModelLedgerSourceEntry.model_validate({**fields, "corrected_by": ()})
    with pytest.raises(ValidationError):
        ModelLedgerSourceEntry.model_validate(
            {**fields, "supersession_status": EnumContextSupersessionStatus.ACTIVE}
        )


def test_operator_span_is_exactly_the_typed_span() -> None:
    quote = quoted_words(SPAN_RULING)
    assert quote == "framing before: use the pool"
    result = _handler(_ListReader((SPAN_RULING, SPAN_CORRECTION))).handle(
        _request("2026-10-03T10:00:00Z")
    )
    (entry,) = result.entries
    assert entry.status is EnumContextSourceStatus.RESOLVED
    assert entry.operator_words == quote[16:28] == "use the pool"
    assert entry.recorded_framing == ("framing before: ", "")
    assert entry.framing_label == FRAMING_LABEL
    (correction,) = entry.corrected_by
    assert correction.operator_span == "16:28"
    assert correction.text == SPAN_CORRECTION


def test_an_operator_span_outside_the_quote_is_ambiguous_not_trimmed() -> None:
    wide = SPAN_CORRECTION.replace("operator_span=16:28", "operator_span=16:99")
    result = _handler(_ListReader((SPAN_RULING, wide))).handle(
        _request("2026-10-03T10:00:00Z")
    )
    (entry,) = result.entries
    assert entry.status is EnumContextSourceStatus.AMBIGUOUS
    assert entry.operator_words is None
    assert entry.framing_label == CORRECTED_LABEL
    assert entry.corrected_by[0].text == wide


def test_refusals_are_typed_never_an_empty_resolved() -> None:
    handler = _handler(_ListReader(DAY))
    denied = handler.handle(_request("2026-10-01T23:59:59Z", tenant="other"))
    assert denied.status is EnumContextSourceStatus.DENIED
    assert not denied.entries
    bad = handler.handle(_request("dev@9d1ed93"))
    assert bad.status is EnumContextSourceStatus.UNAVAILABLE
    assert bad.error_code is EnumContextSourceErrorCode.INVALID_REQUEST
    other = handler.handle(
        _request("2026-10-01T23:59:59Z", factor=EnumContextFactor.ADRS)
    )
    assert other.status is EnumContextSourceStatus.UNRESOLVED
    empty = handler.handle(_request("2026-09-30T00:00:00Z"))
    assert empty.status is EnumContextSourceStatus.UNRESOLVED
    assert not empty.entries
    broken = _handler(_BrokenReader()).handle(_request("2026-10-01T23:59:59Z"))
    assert broken.status is EnumContextSourceStatus.UNAVAILABLE
    assert not broken.entries


def test_contract_tenants_default_to_none_so_an_unbound_overlay_denies() -> None:
    handler = HandlerContextSourceLedger(reader=_ListReader(DAY))
    result = handler.handle(_request("2026-10-01T23:59:59Z"))
    assert result.status is EnumContextSourceStatus.DENIED


def test_node_answers_the_context_source_contract() -> None:
    node = NodeContextSourceLedgerEffect(reader=_ListReader(DAY), tenant_ids=(TENANT,))
    assert node.synchronous_execution is True
    assert (
        node.terminal_event == "onex.evt.omnimarket.context-source-ledger-completed.v1"
    )
    result = node.handle(_request("2026-10-01T23:59:59Z"))
    assert isinstance(result, ModelContextSourceResult)
    roundtrip = ModelLedgerSourceResult.model_validate_json(result.model_dump_json())
    assert roundtrip == result
