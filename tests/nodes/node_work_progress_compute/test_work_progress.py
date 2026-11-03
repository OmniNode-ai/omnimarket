# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overlay-supplied budgets drive deterministic work progress findings."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_work_progress_compute import NodeWorkProgressCompute
from omnimarket.nodes.node_work_progress_compute.handlers import handler_work_progress
from omnimarket.nodes.node_work_progress_compute.models import (
    ModelWorkProgressRequest,
    ModelWorkProgressSubject,
)
from omnimarket.nodes.node_work_progress_compute.overlay import (
    NODE_NAME,
    load_budgets,
    load_phase_notes,
    load_phases,
)
from tests.node_overlay_support import install_node_overlay

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def budgets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, int]:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"budget_seconds": {"placed": 600, "claimed": 3600, "pr_open": 7200}},
    )
    return load_budgets()


def _subject(
    lane: str,
    phase: str,
    spent: float,
    *,
    row: str = "row-1",
    owner: str = "orchestrator-31d9",
) -> ModelWorkProgressSubject:
    return ModelWorkProgressSubject(
        lane=lane,
        phase=phase,
        entered_at=NOW - timedelta(seconds=spent),
        entering_row_id=row,
        owner=owner,
    )


def test_one_over_budget_lane_in_each_phase(budgets: dict[str, int]) -> None:
    request = ModelWorkProgressRequest(
        now=NOW,
        budgets=budgets,
        phase_notes=load_phase_notes(),
        subjects=(
            _subject("placed-lane", "placed", 601),
            _subject("claimed-lane", "claimed", 3840),
            _subject("pr-lane", "pr_open", 7201),
            _subject("under-lane", "claimed", 3599),
            _subject("at-lane", "placed", 600),
        ),
    )
    findings = NodeWorkProgressCompute().handle(request).findings
    assert len(findings) == 3
    assert [finding.lane for finding in findings] == [
        "placed-lane",
        "claimed-lane",
        "pr-lane",
    ]


def test_finding_fields_and_exact_text(budgets: dict[str, int]) -> None:
    request = ModelWorkProgressRequest(
        now=NOW,
        budgets=budgets,
        phase_notes=load_phase_notes(),
        subjects=(
            _subject("rf0925-c2", "claimed", 3840),
            _subject("rf0925-c3", "placed", 660.9, owner="worker-72"),
        ),
    )
    claimed, placed = NodeWorkProgressCompute().handle(request).findings
    assert (
        claimed.lane,
        claimed.phase,
        claimed.entering_row_id,
        claimed.budget_seconds,
        claimed.spent_seconds,
        claimed.owner,
    ) == ("rf0925-c2", "claimed", "row-1", 3600, 3840, "orchestrator-31d9")
    assert claimed.text == (
        "Lane rf0925-c2 has been in claimed for 64 minutes against a 60 minute "
        "budget; no PR is recorded in the ledger. Owner: orchestrator-31d9."
    )
    assert placed.spent_seconds == 660
    assert placed.owner == "worker-72"
    assert placed.text == (
        "Lane rf0925-c3 has been in placed for 11 minutes against a 10 minute "
        "budget. Owner: worker-72."
    )


@pytest.mark.parametrize("offset", [-1, 0, 0.5, 1])
def test_strict_budget_boundary(budgets: dict[str, int], offset: float) -> None:
    request = ModelWorkProgressRequest(
        now=NOW,
        budgets=budgets,
        subjects=(_subject("boundary", "placed", budgets["placed"] + offset),),
    )
    findings = NodeWorkProgressCompute().handle(request).findings
    assert bool(findings) is (offset > 0)
    if findings:
        assert findings[0].spent_seconds == int(budgets["placed"] + offset)


def test_future_subject_and_empty_input(budgets: dict[str, int]) -> None:
    node = NodeWorkProgressCompute()
    assert (
        node.handle(
            ModelWorkProgressRequest(now=NOW, budgets=budgets, subjects=())
        ).findings
        == ()
    )
    assert (
        node.handle(
            ModelWorkProgressRequest(
                now=NOW,
                budgets=budgets,
                subjects=(_subject("future", "placed", -60),),
            )
        ).findings
        == ()
    )


def test_keys_distinguish_entries_and_repeat_deterministically(
    budgets: dict[str, int],
) -> None:
    request = ModelWorkProgressRequest(
        now=NOW,
        budgets=budgets,
        subjects=(
            _subject("rf0925-c2", "claimed", 3840, row="entry-a"),
            _subject("rf0925-c2", "claimed", 3840, row="entry-b"),
        ),
    )
    node = NodeWorkProgressCompute()
    first = node.handle(request)
    assert first == node.handle(request)
    assert [finding.idempotency_key for finding in first.findings] == [
        "work-progress|lane:rf0925-c2|claimed|entry-a",
        "work-progress|lane:rf0925-c2|claimed|entry-b",
    ]


@pytest.mark.parametrize("field", ["lane", "phase", "entering_row_id"])
def test_key_separator_refused(field: str) -> None:
    fields = _subject("lane", "placed", 601).model_dump()
    fields[field] = "bad|value"
    with pytest.raises(ValidationError, match=field):
        ModelWorkProgressSubject.model_validate(fields)


@pytest.mark.parametrize("field", ["lane", "phase", "entering_row_id", "owner"])
def test_empty_subject_fields_refused(field: str) -> None:
    fields = _subject("lane", "placed", 601).model_dump()
    fields[field] = ""
    with pytest.raises(ValidationError, match=field):
        ModelWorkProgressSubject.model_validate(fields)


@pytest.mark.parametrize("field", ["now", "entered_at"])
def test_naive_datetime_refused(budgets: dict[str, int], field: str) -> None:
    fields = ModelWorkProgressRequest(
        now=NOW,
        budgets=budgets,
        subjects=(_subject("lane", "placed", 601),),
    ).model_dump()
    if field == "now":
        fields["now"] = NOW.replace(tzinfo=None)
    else:
        fields["subjects"][0]["entered_at"] = NOW.replace(tzinfo=None)
    with pytest.raises(ValidationError, match=field):
        ModelWorkProgressRequest.model_validate(fields)


def test_missing_request_phase_budget_refused(budgets: dict[str, int]) -> None:
    with pytest.raises(ValidationError, match="no budget for phase 'other'"):
        ModelWorkProgressRequest(
            now=NOW,
            budgets=budgets,
            subjects=(_subject("lane", "other", 601),),
        )


@pytest.mark.parametrize("phase", ["placed", "claimed", "pr_open"])
def test_missing_overlay_phase_budget_refused(
    budgets: dict[str, int],
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reduced = {name: value for name, value in budgets.items() if name != phase}
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, {"budget_seconds": reduced})
    with pytest.raises(
        NodeOverlayError,
        match=rf"{NODE_NAME}\.budget_seconds\.{phase}.*ONEX_SKILL_OVERLAY_ROOTS",
    ):
        load_budgets()


@pytest.mark.parametrize("value", [0, -1])
def test_nonpositive_budgets_refused(
    budgets: dict[str, int],
    value: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    changed = {**budgets, "claimed": value}
    with pytest.raises(ValidationError, match="claimed"):
        ModelWorkProgressRequest(now=NOW, budgets=changed, subjects=())
    install_node_overlay(monkeypatch, tmp_path, NODE_NAME, {"budget_seconds": changed})
    with pytest.raises(NodeOverlayError, match=r"budget_seconds\.claimed"):
        load_budgets()


def test_overlay_budget_changes_findings(
    budgets: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    subjects = (_subject("rf0925-c2", "claimed", 3840),)
    before = NodeWorkProgressCompute().handle(
        ModelWorkProgressRequest(
            now=NOW,
            budgets=load_budgets(),
            subjects=subjects,
        )
    )
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {
            "budget_seconds": {**budgets, "claimed": 4000},
        },
    )
    after = NodeWorkProgressCompute().handle(
        ModelWorkProgressRequest(
            now=NOW,
            budgets=load_budgets(),
            subjects=subjects,
        )
    )
    assert [finding.lane for finding in before.findings] == ["rf0925-c2"]
    assert after.findings == ()


def test_missing_overlay_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    with pytest.raises(NodeOverlayError, match=NODE_NAME):
        load_budgets(environ={})


@pytest.mark.parametrize(
    "document",
    [
        "not a mapping\n",
        "budget_seconds: {placed: 600, claimed: '1800', pr_open: 3600}\n",
        "budget_seconds: {placed: 600, claimed: 1800, pr_open: 3600, unknown: 1}\n",
    ],
)
def test_malformed_overlay_refused(
    document: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = install_node_overlay(monkeypatch, tmp_path, NODE_NAME, {})
    path.write_text(document, encoding="utf-8")
    with pytest.raises(NodeOverlayError, match=NODE_NAME):
        load_budgets()
    if "unknown" in document:
        with pytest.raises(
            NodeOverlayError, match=rf"{NODE_NAME}\.budget_seconds\.unknown"
        ):
            load_budgets()


def test_no_packaged_budget_values() -> None:
    package = files(f"omnimarket.nodes.{NODE_NAME}")
    contract_text = package.joinpath("contract.yaml").read_text("utf-8")
    assert "budget_seconds" not in contract_text
    contract = yaml.safe_load(contract_text)
    assert contract["config"]["work_progress"]["phases"] == [
        {"name": "placed"},
        {"name": "claimed", "note": "no PR is recorded in the ledger"},
        {"name": "pr_open"},
    ]
    assert [phase.name for phase in load_phases()] == ["placed", "claimed", "pr_open"]
    example = package.joinpath("overlay.example.yaml")
    assert example.is_file()
    assert set(yaml.safe_load(example.read_text("utf-8"))["budget_seconds"]) == {
        "placed",
        "claimed",
        "pr_open",
    }
    assert not package.joinpath("overlay.yaml").is_file()


def test_handler_is_pure() -> None:
    source = inspect.getsource(handler_work_progress)
    for forbidden in ("datetime.now", "open(", "os.environ"):
        assert forbidden not in source
