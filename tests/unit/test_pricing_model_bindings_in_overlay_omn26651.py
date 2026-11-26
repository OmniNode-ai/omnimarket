# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pricing and brief model ids sit in the contract overlay tree, not in source (OMN-26651).

The hard-coded model guard treats ``src`` as source and the contract overlay tree as the
sanctioned home of model ids. The three bindings OMN-21805 added live in the overlay
file; the reader still answers them.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers import model_binding_reader as reader

_ROOT = Path(__file__).resolve().parents[2]
_OVERLAY = next(_ROOT.glob("config/onex/*/pricing_model_bindings.v1.yaml"))
_SOURCE_FILE = _ROOT / "src" / "omnimarket" / "configs" / "model_bindings.v1.yaml"


def test_overlay_file_declares_the_pricing_and_brief_bindings() -> None:
    declared = yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))
    assert declared["frontier_comparison_models"]
    assert declared["bindings"]["pricing_baseline_model"]
    assert declared["bindings"]["lane_dispatch_brief_model"]


def test_source_bindings_file_no_longer_declares_them() -> None:
    declared = yaml.safe_load(_SOURCE_FILE.read_text(encoding="utf-8"))
    assert "frontier_comparison_models" not in declared
    assert "pricing_baseline_model" not in declared["bindings"]
    assert "lane_dispatch_brief_model" not in declared["bindings"]


def test_reader_answers_from_the_overlay() -> None:
    overlay = yaml.safe_load(_OVERLAY.read_text(encoding="utf-8"))
    assert (
        reader.model_binding("pricing_baseline_model")
        == (overlay["bindings"]["pricing_baseline_model"])
    )
    assert tuple(reader.model_binding_section("frontier_comparison_models")) == tuple(
        overlay["frontier_comparison_models"]
    )


def test_reader_still_fails_closed_on_a_missing_binding() -> None:
    import pytest

    with pytest.raises(KeyError):
        reader.model_binding("no_such_binding_omn26651")
