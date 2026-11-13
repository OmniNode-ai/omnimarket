# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold shapes beyond the 22 parity cases, and the seams of the pause check command (OMN-24233).

Seams: the hold verdict reasons that let the command fall back to the file (a reason it renames
makes the fallback silently never happen, or happen when it must not); the holds line shape the
reader answers and the command parses back into a HOLD finding; the topics of the contract; the
rules document the drain map hands in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.adapters import (
    InMemoryHoldVerdictSink,
)
from omnimarket.nodes.node_ledger_hold_verdict_effect.handlers import HandlerHoldVerdict
from omnimarket.nodes.node_ledger_hold_verdict_effect.models import (
    EnumHoldVerdict,
    ModelHoldVerdictOverlay,
    ModelHoldVerdictRequest,
)
from omnimarket.nodes.node_pause_check_findings_compute.handlers import (
    HandlerPauseCheckFindings,
)
from omnimarket.nodes.node_pause_check_findings_compute.models import (
    EnumFindingKind,
    EnumPauseVerdict,
    ModelFinding,
    ModelPauseCheckOverlay,
    ModelPauseCheckPr,
    ModelPauseCheckRequest,
    ModelPauseCheckRules,
)
from omnimarket.nodes.node_pause_check_runtime_effect import runtime_pause_check
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from tests.unit.nodes.node_ledger_hold_verdict_effect.test_hold_verdict_reader_seam import (
    pr_hold_entity,
)
from tests.unit.nodes.node_ledger_hold_verdict_effect.test_runtime_hold_verdict import (
    READER_OVERLAY,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    FakeStateReader,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    QueryNodePointRead,
)

pytestmark = pytest.mark.unit

NOW = "2026-11-13T12:00:00Z"
OVERLAY = ModelPauseCheckOverlay(
    snippet_bytes=220,
    max_order_tokens=8,
    ledger_env="ONEX_PAUSE_CHECK_LEDGER_FILE",
    journal_env="ONEX_PAUSE_CHECK_JOURNAL",
)
RULES = ModelPauseCheckRules(
    report_types=("STATUS", "TERMINAL", "RELEASE", "FRICTION", "CORRECTION", "ACK"),
    landing_order="landing-order",
    parked={},
    repo_pause_lines={},
)


def decide(rows: list[str], lane: str | None = None) -> tuple[str, ...]:
    request = ModelPauseCheckRequest(
        prs=(ModelPauseCheckPr(repo="omnimarket", number=7, lane=lane),),
        rules=RULES,
        ledger_text="\n".join(rows) + "\n",
        now=NOW,
        full_scan=False,
    )
    return HandlerPauseCheckFindings(OVERLAY).handle(request).results[0].render_lines()


def test_a_legacy_hold_with_no_id_pauses_as_a_pr_row_until_a_release_names_the_pr() -> (
    None
):
    hold = "2026-11-10T10:00:00Z | HOLD | lane=orch-1 | omnimarket#7 do not touch"
    release = "2026-11-10T11:00:00Z | RELEASE | lane=orch-1 | omnimarket#7 released"
    assert decide([hold])[0].startswith("PR-ROW 1:")
    assert decide([hold, release])[-1].startswith("VERDICT: CLEAR")


def test_a_release_naming_one_pr_lifts_a_repo_hold_for_that_pr_only() -> None:
    hold = "2026-11-10T10:00:00Z | HOLD | id=h1 | to=all | repo=omnimarket | until=2026-12-01T00:00Z"
    release = "2026-11-10T11:00:00Z | RELEASE | re=h1 | pr=omnimarket#7"
    assert decide([hold])[0].startswith("HOLD 1:")
    assert decide([hold, release])[-1].startswith("VERDICT: CLEAR")
    other = ModelPauseCheckRequest(
        prs=(ModelPauseCheckPr(repo="omnimarket", number=8),),
        rules=RULES,
        ledger_text=hold + "\n" + release + "\n",
        now=NOW,
        full_scan=False,
    )
    assert (
        HandlerPauseCheckFindings(OVERLAY).handle(other).results[0].verdict
        is EnumPauseVerdict.PAUSED
    )


def test_a_snippet_never_splits_a_multibyte_character() -> None:
    row = "2026-11-10T10:00:00Z | HOLD | id=h1 | pr=omnimarket#7 | " + "é" * 200
    line = decide([row])[0]
    assert line.startswith("HOLD 1:")
    assert len(line.split(": ", 1)[1].encode()) <= 220
    assert "�" not in line


def test_bus_holds_replace_the_file_holds_and_recompute_the_verdict() -> None:
    row = "2026-11-10T10:00:00Z | HOLD | id=h1 | pr=omnimarket#7"
    request = ModelPauseCheckRequest(
        prs=(ModelPauseCheckPr(repo="omnimarket", number=7),),
        rules=RULES,
        ledger_text=row + "\n",
        now=NOW,
        full_scan=False,
    )
    result = HandlerPauseCheckFindings(OVERLAY).handle(request).results[0]
    assert result.verdict is EnumPauseVerdict.PAUSED
    cleared = result.with_holds(())
    assert (cleared.verdict, cleared.stops) == (EnumPauseVerdict.CLEAR, 0)
    held = cleared.with_holds(
        (ModelFinding(kind=EnumFindingKind.HOLD, text="db:1: by=x scope=<> until=y"),)
    )
    assert (held.verdict, held.stops) == (EnumPauseVerdict.PAUSED, 1)


# ---- seam: the pause check command <-> the hold verdict node ------------------------------------


def verdict_for(state: FakeStateReader, *, allowed: bool = True):
    reader = HandlerWorkLedgerReader(
        _reader_overlay(),
        QueryNodePointRead(state),
        InMemoryEventSink(),
        clock=lambda: datetime(2026, 11, 13, 12, tzinfo=UTC),
    )
    answer = reader.handle(
        ModelReaderRequest(reader="pause-check", query=EnumReaderQuery.HOLDS)
    )
    handler = HandlerHoldVerdict(
        ModelHoldVerdictOverlay(file_fallback_allowed=allowed, journal_env="J"),
        InMemoryHoldVerdictSink(),
    )
    verdict = handler.handle(
        ModelHoldVerdictRequest(
            pr="omnimarket#7", lane=None, node_answer=answer, file_fallback_lines=None
        )
    )
    return answer, verdict


def _reader_overlay():
    from omnimarket.nodes.node_work_ledger_reader_effect.models import (
        ModelReaderOverlay,
    )

    return ModelReaderOverlay.model_validate(yaml.safe_load(READER_OVERLAY))


def test_the_reasons_that_allow_a_file_fallback_are_the_hold_verdict_reasons() -> None:
    from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
        WorkLedgerReadError,
    )

    _, fault = verdict_for(FakeStateReader(error=WorkLedgerReadError("down")))
    assert fault.verdict is EnumHoldVerdict.UNDECIDED
    assert fault.reason in runtime_pause_check._NOT_ANSWERED
    _, retired = verdict_for(
        FakeStateReader(error=WorkLedgerReadError("down")), allowed=False
    )
    assert retired.reason == "file-fallback-retired"
    assert retired.reason not in runtime_pause_check._NOT_ANSWERED
    assert "unparseable-hold-line" not in runtime_pause_check._NOT_ANSWERED
    assert EnumReaderStatus.OK is not None


def test_a_holds_line_of_the_reader_parses_back_into_a_hold_finding() -> None:
    answer, verdict = verdict_for(FakeStateReader((pr_hold_entity(),)))
    assert verdict.verdict is EnumHoldVerdict.HELD
    (finding,) = runtime_pause_check._bus_holds(answer.lines, verdict.hold_ids)
    assert finding.kind is EnumFindingKind.HOLD
    assert finding.text.startswith("hold-a: by=")
    assert "bus hold" not in finding.text


# ---- seam: the contract topics and the rules document -------------------------------------------


def test_the_contract_topics_are_the_ones_the_events_carry() -> None:
    topics = runtime_pause_check.load_topics()
    assert set(topics) == {"pause_check_answered", "pause_check_refused"}
    contract = yaml.safe_load(
        (Path(runtime_pause_check.__file__).parent / "contract.yaml").read_text()
    )
    assert sorted(contract["externally_consumed_topics"]) == sorted(topics.values())


def test_the_rules_document_is_exactly_the_four_cells_the_drain_map_hands_in() -> None:
    assert set(ModelPauseCheckRules.model_fields) == {
        "report_types",
        "landing_order",
        "parked",
        "repo_pause_lines",
    }
