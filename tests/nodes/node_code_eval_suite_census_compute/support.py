# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders for the node_code_eval_suite_census_compute tests."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.code_eval_suite.model_suite_overlay import (
    ModelCodeEvalSuiteCensusOverlay,
)
from omnimarket.models.code_eval_suite.model_suite_task import (
    ModelCodeEvalSuiteCheck,
    ModelCodeEvalSuiteTask,
)
from omnimarket.models.delegation.model_size_band import EnumSizeBand
from omnimarket.nodes.node_code_contract_score_effect.models import (
    EnumCodeScoreSource,
    EnumCodeScoreVerdict,
)

PASS = EnumCodeScoreVerdict.PASS
FAIL = EnumCodeScoreVerdict.FAIL_CHECK
UNSCORABLE = EnumCodeScoreVerdict.UNSCORABLE
FALSIFIER_NOT_HELD = EnumCodeScoreSource.FALSIFIER_NOT_HELD

_counter = itertools.count(1_000_000)


def _commit(seed: int | str) -> str:
    """A 40-hex commit: an int is spelled out, a string is used as given."""
    return seed if isinstance(seed, str) else f"{seed:040x}"


def task(
    repository: str = "repo-a",
    merge_commit: int | str | None = None,
    contract_id: str | None = None,
    band: EnumSizeBand = EnumSizeBand.S,
    control: EnumCodeScoreVerdict = EnumCodeScoreVerdict.PASS,
    control_source: EnumCodeScoreSource | None = None,
) -> ModelCodeEvalSuiteTask:
    """A valid task; a merge commit or contract id left out is made unique."""
    if merge_commit is None:
        merge_commit = next(_counter)
    if contract_id is None:
        contract_id = f"OMN-{merge_commit}"
    return ModelCodeEvalSuiteTask(
        repository=repository,
        merge_commit=_commit(merge_commit),
        base_commit=_commit(
            int(merge_commit, 16) + 1
            if isinstance(merge_commit, str)
            else merge_commit + 1
        ),
        contract_id=contract_id,
        subject="subject",
        merged_at="2026-01-01T00:00:00Z",
        files_changed=1,
        lines_changed=1,
        band=band,
        checks=(ModelCodeEvalSuiteCheck(item_id="i", check_type="t", check_value="v"),),
        check_files=("a.py",),
        held_paths=("a.py",),
        control=control,
        control_source=control_source,
    )


def overlay() -> ModelCodeEvalSuiteCensusOverlay:
    path = (
        Path(__file__).resolve().parent
        / "fixtures"
        / "overlays"
        / "node_code_eval_suite_census_compute"
        / "overlay.yaml"
    )
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelCodeEvalSuiteCensusOverlay.model_validate(data)
