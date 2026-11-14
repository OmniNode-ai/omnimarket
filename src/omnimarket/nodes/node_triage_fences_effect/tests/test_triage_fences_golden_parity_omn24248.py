# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The moved fence effect node prints what the node in the frozen repository printed (OMN-24248).

``fixtures/triage_fences_module_goldens.json`` holds the reports the standalone fence module printed
for the cases below, recorded before it was deleted. The effect node here, run with an overlay that
holds the old 48 hour window and the old 6 hour pull-request window, must print the same report, key
for key and in the same order, apart from the new trailing ``live_basis`` key of each claim.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_triage_fences_effect.handlers.handler_triage_fences import (
    HandlerTriageFences,
)
from omnimarket.nodes.node_triage_fences_effect.models import (
    ModelTriageFencesOptions,
    ModelTriageFencesOverlay,
    ModelTriageFencesRequest,
)

from .triage_fences_world import NOW, OMN_15954_LEDGER_LINE


def _reader_none(repo: str) -> list[dict[str, Any]]:
    return []


def _handler(
    ledger: Path, reader: Callable[[str], list[dict[str, Any]]] = _reader_none
) -> HandlerTriageFences:
    receipts = ledger.parent / "receipts"
    receipts.mkdir(exist_ok=True)
    overlay = ModelTriageFencesOverlay(
        stale_after_minutes=48 * 60,
        pr_window_hours=6,
        repos=("omnibase_core",),
        receipt_dir=receipts,
        running_receipt_max_age_minutes=240,
        watcher_state=None,
    )
    return HandlerTriageFences(
        ModelTriageFencesOptions(ledger=ledger), overlay, pull_reader=reader
    )


def _write(tmp_path: Path, *lines: str) -> Path:
    path = tmp_path / "LEDGER.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- parity with the module's report ----------------------------------------------------------


def _pulls_reader(repo: str) -> list[dict[str, Any]]:
    if repo == "omnibase_infra":
        return [
            {
                "number": 3156,
                "title": "fix(OMN-17562): kernel stops subscribing",
                "headRefName": "jonah/omn-17562-kernel-stops",
                "updatedAt": "2026-09-03T10:20:51Z",
                "url": "https://github.com/OmniNode-ai/omnibase_infra/pull/3156",
            },
            {
                "number": 3153,
                "title": "chore: advance contract pin",
                "headRefName": "jonah/omn-17292-pin-bump",
                "updatedAt": "2026-09-03T06:46:15Z",
                "url": "https://github.com/OmniNode-ai/omnibase_infra/pull/3153",
            },
            {
                "number": 9,
                "title": "chore(OMN-10000): far too old to fence",
                "headRefName": "jonah/omn-10000-old",
                "updatedAt": "2026-08-01T00:00:00Z",
                "url": "https://github.com/OmniNode-ai/omnibase_infra/pull/9",
            },
            {
                "number": 12,
                "title": "two tickets OMN-1001 and OMN-1002 in one title",
                "headRefName": "x/no-ticket-here",
                "updatedAt": "not-a-timestamp",
                "url": None,
            },
        ]
    if repo == "omnimarket":
        raise OSError("gh exploded for omnimarket")
    return []


@dataclass
class Case:
    lines: list[str]
    kwargs: dict[str, Any] = field(default_factory=dict)
    reader: Callable[[str], list[dict[str, Any]]] = _reader_none


ROW_FORMS = [
    "- 2026-09-01T18:47:31Z CLAIM lane=omn17060-sibling-provenance-gate actor=claude "
    "ticket=OMN-17060 scope=omnibase_infra verifier work.",
    "| 2026-08-31T00:39:00Z | OMN-16804-chain-leg1 | OMN-16930 | CLAIM | Leg 1 starting. |",
    "2026-09-02T17:44:26Z | codex-root-omn17519-pr1146-close | OMN-17519 | CLAIM | repo=x",
    "2026-09-02T00:06:40Z | CLAIM | codex:/root/forwarder_v2_resume | OMN-17013 OCC correction",
    "2026-09-01T12:13:30Z CLAIM lane=occ-seam actor=codex ticket=OMN-17483 related=OMN-16360",
    "2026-09-01T18:32:39Z [codex:/root/beta_plan_audit] CLAIM OMN-16999 scope=worktree:x",
    "2026-09-01T22:49:00Z RECOVERY/CLAIM lane=codex-root-x actor=codex ticket=OMN-17013",
]

CASES: dict[str, Case] = {
    "empty-ledger": Case([]),
    "dangling-claim": Case([OMN_15954_LEDGER_LINE]),
    "every-row-form": Case(ROW_FORMS),
    "terminal-closes-its-claim": Case(
        [
            "- 2026-09-02T08:00:00Z CLAIM lane=alpha actor=claude ticket=OMN-1001 scope=x",
            "- 2026-09-02T09:00:00Z TERMINAL lane=alpha actor=claude ticket=OMN-1001 outcome=done",
            "- 2026-09-02T10:00:00Z CLAIM lane=beta actor=claude ticket=OMN-1002 scope=y",
        ]
    ),
    "claim-update-chain": Case(
        [
            "- 2026-09-02T08:00:00Z CLAIM lane=alpha actor=claude ticket=OMN-1001 scope=x",
            "- 2026-09-02T10:00:00Z CLAIM_UPDATE lane=alpha actor=claude ticket=OMN-1001 status=b",
            "- 2026-09-02T11:00:00Z CLAIM_UPDATE lane=alpha actor=claude ticket=OMN-1001 status=c",
        ]
    ),
    "stale-and-live": Case(
        [
            "2026-08-24T05:00:00Z | CLAIM | lane=dod-closeout-chunk0 | ticket=OMN-19401 | actor=x",
            "2026-09-03T11:00:00Z | CLAIM | lane=alive | ticket=OMN-1001 | actor=x",
            "2026-08-24T05:00:00Z | CLAIM | lane=orchestrator-a | ticket=OMN-1002 | actor=x",
            "2026-09-03T09:00:00Z | STATUS | lane=orchestrator-a | still working",
            "2026-08-24T05:00:00Z | CLAIM | lane=old | ticket=OMN-1003 | actor=x",
            "2026-09-03T09:00:00Z | STATUS | lane=reader | read the CLAIM of lane=old",
        ]
    ),
    "narrow-staleness": Case([OMN_15954_LEDGER_LINE], {"stale_after_hours": 24.0}),
    "unparsed-row": Case(
        [
            "- 2026-09-02T08:00:00Z CLAIM~~ lane=alpha ticket=OMN-1001 malformed kind",
            "- 2026-09-02T09:00:00Z CLAIM lane=beta actor=x ticket=OMN-1002 scope=ok",
        ]
    ),
    "open-prs": Case(
        [OMN_15954_LEDGER_LINE],
        {
            "include_open_prs": True,
            "repos": ["omnibase_infra", "omnimarket"],
            "window_hours": 6,
        },
        _pulls_reader,
    ),
    "open-prs-narrow-window": Case(
        ["2026-09-02T09:00:00Z CLAIM lane=gamma actor=codex ticket=OMN-1002 scope=x"],
        {"include_open_prs": True, "repos": ["omnibase_infra"], "window_hours": 1},
        _pulls_reader,
    ),
}


def _node_request(case: Case) -> ModelTriageFencesRequest:
    kwargs = case.kwargs
    values: dict[str, Any] = {
        "now": NOW,
        "include_open_prs": kwargs.get("include_open_prs", False),
    }
    if "repos" in kwargs:
        values["repos"] = tuple(kwargs["repos"])
    if "window_hours" in kwargs:
        values["pr_window_hours"] = kwargs["window_hours"]
    if "stale_after_hours" in kwargs:
        values["stale_after_hours"] = kwargs["stale_after_hours"]
    return ModelTriageFencesRequest(**values)


GOLDENS = json.loads(
    (
        Path(__file__).parent / "fixtures" / "triage_fences_module_goldens.json"
    ).read_text()
)


def _golden_report(name: str, path: Path) -> str:
    return json.dumps({**GOLDENS["reports"][name], "ledger": str(path)}, indent=2)


def _without_live_basis(report: dict[str, Any]) -> dict[str, Any]:
    """The goldens predate the new trailing live_basis key of a claim; drop it to compare."""
    for section in ("open_claims", "stale_claims"):
        report[section] = [
            {k: v for k, v in claim.items() if k != "live_basis"}
            for claim in report[section]
        ]
    return report


@pytest.mark.parametrize("name", sorted(CASES))
def test_node_report_matches_what_the_deleted_module_printed(
    tmp_path: Path, name: str
) -> None:
    case = CASES[name]
    path = _write(tmp_path, *case.lines)
    result = _handler(path, case.reader).handle(_node_request(case))
    assert result.exit_code == 0
    assert result.report is not None
    actual = json.dumps(
        _without_live_basis(result.report.model_dump(mode="json")), indent=2
    )
    assert actual == _golden_report(name, path)


def test_the_goldens_cover_every_case_and_nothing_else() -> None:
    assert set(GOLDENS["reports"]) == set(CASES)


def test_the_cases_between_them_reach_every_report_section() -> None:
    """Positive control: the recorded cases are not all one shape."""
    reports = list(GOLDENS["reports"].values())
    assert any(r["stale_claims"] for r in reports)
    assert any(r["open_claims"] for r in reports)
    assert any(r["unparsed_rows"] for r in reports)
    assert any(r["errors"] for r in reports)
    assert any(r["open_pr_fences"] for r in reports)
    assert any(not f["within_window"] for r in reports for f in r["open_pr_fences"])
    assert any(f["updated_at"] is None for r in reports for f in r["open_pr_fences"])
    assert any(len(r["fenced_tickets"]) > 3 for r in reports)
