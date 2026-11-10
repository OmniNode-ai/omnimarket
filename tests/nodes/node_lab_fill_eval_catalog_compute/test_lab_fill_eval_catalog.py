# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The eval catalog check: scenario set, the injected break (the pre-change catalog), and the CLI's exit codes."""

from __future__ import annotations

import io
import json
from importlib.resources import files

import pytest
import yaml

from omnimarket.nodes.node_lab_fill_eval_catalog_compute.handlers import (
    HandlerLabFillEvalCatalog,
)
from omnimarket.nodes.node_lab_fill_eval_catalog_compute.models import (
    EnumReadinessScenario,
    ModelEvalCatalogRequest,
)
from omnimarket.nodes.node_lab_fill_eval_catalog_compute.runtime_lab_fill_eval_catalog_cli import (
    main,
)

pytestmark = pytest.mark.unit

OVERLAY = files("omnimarket.nodes.node_lab_fill_eval_catalog_compute").joinpath(
    "overlay.example.yaml"
)


def _doc() -> dict:
    return yaml.safe_load(OVERLAY.read_text())


def test_the_allowed_set_is_r1_to_r9_and_experiment() -> None:
    assert {s.value for s in EnumReadinessScenario} == {
        f"R{n}" for n in range(1, 10)
    } | {"experiment"}


@pytest.mark.parametrize("scenario", [*(f"R{n}" for n in range(1, 10)), "experiment"])
def test_each_allowed_value_passes(scenario: str) -> None:
    doc = _doc()
    for e in doc["evals"]:
        e["feeds"] = scenario
    assert (
        HandlerLabFillEvalCatalog()
        .handle(ModelEvalCatalogRequest.model_validate(doc))
        .ok
        is True
    )


@pytest.mark.parametrize("bad", ["R0", "R10", "r3", "Experiment", "", "R3 "])
def test_a_value_outside_the_set_fails(bad: str) -> None:
    doc = _doc()
    doc["evals"][0]["feeds"] = bad
    result = HandlerLabFillEvalCatalog().handle(
        ModelEvalCatalogRequest.model_validate(doc)
    )
    assert result.ok is False
    assert [d.eval_id for d in result.defects] == [doc["evals"][0]["id"]]


def test_injected_break_the_catalog_without_any_feeds_fails_every_eval() -> None:
    # the pre-change catalog: seven evals, none declaring feeds
    doc = _doc()
    for e in doc["evals"]:
        del e["feeds"]
    result = HandlerLabFillEvalCatalog().handle(
        ModelEvalCatalogRequest.model_validate(doc)
    )
    assert result.ok is False
    assert len(result.defects) == 7


def _run(
    monkeypatch: pytest.MonkeyPatch, text: str, args: list[str] | None = None
) -> int:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    return main(args or [])


def test_cli_exits_0_on_the_overlay_1_on_a_defect_2_on_unreadable_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(monkeypatch, OVERLAY.read_text()) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    doc = _doc()
    del doc["evals"][3]["feeds"]
    assert _run(monkeypatch, yaml.safe_dump(doc)) == 1
    assert (
        json.loads(capsys.readouterr().out)["defects"][0]["eval_id"]
        == doc["evals"][3]["id"]
    )

    assert _run(monkeypatch, "evals: [unclosed") == 2
    assert "unreadable" in capsys.readouterr().err
    assert _run(monkeypatch, "evals: not-a-list") == 2
    assert _run(monkeypatch, "", ["unexpected"]) == 2
