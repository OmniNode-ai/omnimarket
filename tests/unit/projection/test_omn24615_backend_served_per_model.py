# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC6, follow-up: every served per-model figure carries the backend as its own field.

AC6: "every served per-model figure is grouped by the model that ran, with the backend served as its own field".
#6321 grouped the local views by the model that ran, and the metering summary serves each model's backends, but the
re-review of #6442 (2026-11-19) found the model-routing, summary and quality-gate views' per-model rows carry no
backend, so a page cannot tell that Beam runs were reached through the byok-reflection alias.

The backend is defined as the metering record defines it: delegated_to, when it is set and differs from the model
that ran. Each per-model row serves the sorted list of the backends its runs were reached through.

Failure modes, each with a test below:
  B1  a per-model row of model-routing (by_model, the model-by-task rows, a tier's models) serves no backend
  B2  the summary's byModel or the quality-gate's per-model tokens serve no backend
  B3  a model reached directly is given its own name, or an empty alias, as a backend
  B4  a tier's model row lists a backend its runs in that tier were not reached through
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _QUALITY,
    _ROUTING,
    _SUMMARY,
    _row,
    _run,
    _store,
)

pytestmark = pytest.mark.unit

_BEAM = "Beam-1.0-501B-A23B"
_QWEN = "Qwen3.8-27B"


def _beam_store(tmp_path: Path) -> Path:
    return _store(
        tmp_path,
        _run(
            "beam-direct",
            delegated_to=_BEAM,
            model_name=_BEAM,
            cost_tier_name="cheap_cloud",
        ),
        _run(
            "beam-byok-1",
            delegated_to="byok-reflection",
            model_name=_BEAM,
            cost_tier_name="byok",
        ),
        _run(
            "beam-byok-2",
            delegated_to="byok-reflection",
            model_name=_BEAM,
            cost_tier_name="byok",
        ),
        _run("qwen-local", delegated_to=_QWEN, model_name=_QWEN),
        _run("qwen-no-alias", delegated_to="", model_name=_QWEN),
    )


def _by_name(rows: list[dict[str, object]], key: str) -> dict[object, object]:
    return {r[key]: r.get("backends") for r in rows}


def test_backend_served_per_model_model_routing(tmp_path: Path) -> None:
    """B1, B3: by_model and the model-by-task rows."""
    routing = _row(_beam_store(tmp_path), _ROUTING)
    expected = {_BEAM: ["byok-reflection"], _QWEN: []}
    assert _by_name(routing["by_model"], "model_name") == expected
    assert _by_name(routing["rows"], "model_name") == expected


def test_backend_served_per_model_each_tier_lists_its_own_backends(
    tmp_path: Path,
) -> None:
    """B1, B4: a tier's model row names only the backends its runs in that tier went through."""
    routing = _row(_beam_store(tmp_path), _ROUTING)
    tiers = {t["cost_tier_name"]: t["models"] for t in routing["by_tier"]["tiers"]}
    assert _by_name(tiers["byok"], "model_name") == {_BEAM: ["byok-reflection"]}
    assert _by_name(tiers["cheap_cloud"], "model_name") == {_BEAM: []}
    assert _by_name(tiers["local"], "model_name") == {_QWEN: []}


def test_backend_served_per_model_summary_and_quality_gate(tmp_path: Path) -> None:
    """B2, B3."""
    path = _beam_store(tmp_path)
    expected = {_BEAM: ["byok-reflection"], _QWEN: []}
    assert _by_name(_row(path, _SUMMARY)["byModel"], "model") == expected
    assert (
        _by_name(_row(path, _QUALITY)["tokens_to_compliance_by_model"], "model_name")
        == expected
    )
