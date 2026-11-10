# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The packaged lab_fill_priority and lab_fill_rank seeds load from omnimarket (OMN-21922).

With no ``ONEX_SKILL_OVERLAY_ROOTS`` in the environment, no operator root is on the path, so
the loader reads the seeds this package carries. The rank seed carries no ``mode``:
the operator sets it, so the rank configuration refuses naming ``mode`` until then.
"""

from __future__ import annotations

import hashlib
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.lab_fill_priority_overlay import (
    PACKAGED_OVERLAY_ROOT,
    PRIORITY_OVERLAY_ID,
    RANK_OVERLAY_ID,
    load_priority_declaration,
    load_rank_config,
)
from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.models.lab_fill.model_lab_fill_priority import ModelLabFillRankConfig

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 2)

# sha256 of the two seed files as first committed (header comments included).
SEED_SHA256 = {
    PRIORITY_OVERLAY_ID: (
        "b8dcc93cb0891f6558a58f2b62ff53d81a5ded3640c7133bb967c1c698b7ec28"
    ),
    RANK_OVERLAY_ID: (
        "0690791dcee7c5de4a1055e228c4b253e6b29b993199337b772b713de20625bd"
    ),
}


def rank_with_mode(tmp_path: Path) -> ModelLabFillRankConfig:
    """The packaged rank seed plus the one key the operator adds: ``mode``."""
    seed = PACKAGED_OVERLAY_ROOT / RANK_OVERLAY_ID / "overlay.yaml"
    document = yaml.safe_load(seed.read_text(encoding="utf-8"))
    assert "mode" not in document
    document["mode"] = "shadow"
    target = tmp_path / RANK_OVERLAY_ID
    target.mkdir()
    (target / "overlay.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    return load_rank_config({OVERLAY_ROOTS_ENV: str(tmp_path)})


def test_lab_fill_priority_overlay_seed_loads_from_omnimarket_in_a_fresh_environment(
    tmp_path: Path,
) -> None:
    declaration = load_priority_declaration(TODAY, {}, rank_with_mode(tmp_path))
    assert declaration.weights.unblock == Fraction(3, 10)
    assert declaration.weights.beta_gate == Fraction(1, 4)
    assert declaration.weights.throughput == Fraction(1, 5)
    assert declaration.weights.priority_age == Fraction(3, 20)
    assert declaration.weights.rework == Fraction(1, 5)
    assert declaration.boost == ()
    assert declaration.exclude == ()


def test_lab_fill_priority_overlay_seed_rank_refuses_naming_mode_while_absent() -> None:
    with pytest.raises(NodeOverlayError, match=r"fields \['mode'\]"):
        load_rank_config({})
    with pytest.raises(NodeOverlayError, match=r"fields \['mode'\]"):
        load_rank_config({OVERLAY_ROOTS_ENV: "  "})


def test_lab_fill_priority_overlay_seed_rank_is_valid_once_the_operator_sets_mode(
    tmp_path: Path,
) -> None:
    config = rank_with_mode(tmp_path)
    assert config.mode == "shadow"
    assert config.blocks_cap == 5


@pytest.mark.parametrize("overlay_id", [PRIORITY_OVERLAY_ID, RANK_OVERLAY_ID])
def test_lab_fill_priority_overlay_seed_bytes_are_the_committed_bytes(
    overlay_id: str,
) -> None:
    seed = PACKAGED_OVERLAY_ROOT / overlay_id / "overlay.yaml"
    assert hashlib.sha256(seed.read_bytes()).hexdigest() == SEED_SHA256[overlay_id]


def test_lab_fill_priority_overlay_named_roots_replace_the_packaged_root(
    tmp_path: Path,
) -> None:
    """A root the environment names is the only source: no packaged fallback behind it."""
    env = {OVERLAY_ROOTS_ENV: str(tmp_path / "empty")}
    with pytest.raises(NodeOverlayError, match=r"lab_fill_priority\.weights"):
        load_priority_declaration(TODAY, env, rank_with_mode(tmp_path))
