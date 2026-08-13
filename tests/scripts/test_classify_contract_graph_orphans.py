# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the orphan-baseline CLI-dispatch classifier (OMN-15984).

A prior audit (OMN-15982) individually re-verified only ~77 of the 688
ORPHANED_PRODUCER/ORPHANED_CONSUMER baseline entries (~11%) before asserting
the mass was "substantially" CLI-dispatch-shim false positives.
``scripts/classify_contract_graph_orphans.py`` closes that sampling gap by
classifying all 688. These tests prove the classifier does the job honestly:
full coverage (not a sample), and both directions of the positive control
actually discriminate (it doesn't just mark everything reachable, and it
doesn't just mark everything an orphan).
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

from omnimarket.validators.contract_topic_graph import (
    CHECKOUT_PACKAGES,
    CHECKOUT_ROOT_ENV,
)

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "classify_contract_graph_orphans.py"
)

pytestmark = pytest.mark.unit


def _checkout_tier_available() -> bool:
    """Same skip guard as test_contract_topic_graph.py's real-corpus tests --
    the classifier needs the SAME checkout tier (omniclaude) build_graph()
    needs, since it re-derives findings from the live graph rather than
    reimplementing graph logic."""
    root = os.environ.get(CHECKOUT_ROOT_ENV)
    if not root:
        return False
    return all((Path(root) / package).is_dir() for package in CHECKOUT_PACKAGES)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "classify_contract_graph_orphans", _SCRIPT
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Pydantic's forward-ref resolution for `ModelOrphanClassification`
    # (module has `from __future__ import annotations`) looks the module up
    # via sys.modules[cls.__module__] -- register it BEFORE exec_module or
    # every model construction inside the loaded module raises
    # PydanticUserError: "is not fully defined".
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> ModuleType:
    return _load_module()


requires_checkout_tier = pytest.mark.skipif(
    not _checkout_tier_available(),
    reason=(
        f"{CHECKOUT_ROOT_ENV} not set or missing an omniclaude/omniintelligence "
        "checkout -- classification needs the full graph, same as the real-corpus "
        "contract_topic_graph tests."
    ),
)


@requires_checkout_tier
def test_classification_covers_every_orphan_entry_exactly_once(mod: ModuleType) -> None:
    """No sampling: every ORPHANED_PRODUCER/ORPHANED_CONSUMER key currently in
    accepted: is classified, and nothing else."""
    entries, baseline_accepted = mod.classify()

    baseline_orphan_keys = {
        k
        for k in baseline_accepted
        if k.startswith("ORPHANED_PRODUCER::") or k.startswith("ORPHANED_CONSUMER::")
    }
    classified_keys = {e.key for e in entries}

    assert classified_keys == baseline_orphan_keys
    # One classification per key -- no duplicate/contradictory tags for the
    # same baseline entry.
    assert len(entries) == len(classified_keys)


@requires_checkout_tier
def test_positive_control_reachable_direction(mod: ModuleType) -> None:
    """aislop_sweep is a confirmed skill_mapping.yaml + SKILL.md backing node
    -- the classifier must not blanket-tag everything genuine_orphan."""
    entries, _ = mod.classify()
    by_key = {e.key: e for e in entries}
    key = (
        "ORPHANED_PRODUCER::aislop_sweep::onex.evt.omnimarket.aislop-sweep-completed.v1"
    )
    assert key in by_key, (
        "aislop_sweep control entry missing from baseline -- update the control"
    )
    assert by_key[key].tag == "cli_dispatch_reachable"
    assert by_key[key].signal is not None


@requires_checkout_tier
def test_positive_control_orphan_direction(mod: ModuleType) -> None:
    """node_ledger_write_effect is the module docstring's own motivating real
    orphan (HandlerLedgerAppend) -- the classifier must not blanket-tag
    everything cli_dispatch_reachable."""
    entries, _ = mod.classify()
    by_key = {e.key: e for e in entries}
    key = "ORPHANED_CONSUMER::node_ledger_write_effect::onex.cmd.platform.ledger-append.v1"
    assert key in by_key, (
        "ledger control entry missing from baseline -- update the control"
    )
    assert by_key[key].tag == "genuine_orphan"
    assert by_key[key].signal is None


@requires_checkout_tier
def test_main_writes_classification_file_and_passes_controls(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = mod.main()
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Positive controls: PASSED" in captured.out
    assert mod.CLASSIFICATION_OUT.is_file()


def test_strip_prefix(mod: ModuleType) -> None:
    assert mod._strip_prefix("node_aislop_sweep") == "aislop_sweep"
    assert mod._strip_prefix("aislop_sweep") == "aislop_sweep"
    assert mod._strip_prefix("node_") == ""


def test_dispatch_command_regex_extracts_node_name(mod: ModuleType) -> None:
    text = "Primary dispatch uses `onex run-node node_ci_watch` (Kafka bus)."
    matches = mod._DISPATCH_COMMAND_RE.findall(text)
    assert matches == ["node_ci_watch"]


def test_architecture_map_regex_extracts_node_name(mod: ModuleType) -> None:
    text = (
        "node_onboarding       -> omnimarket/nodes/node_onboarding/ (policy resolution)"
    )
    matches = mod._ARCHITECTURE_MAP_RE.findall(text)
    assert matches[0] == "node_onboarding"


def test_backing_phrase_matches_across_line_wrap(mod: ModuleType) -> None:
    """Regression guard for the authorize/SKILL.md miss: "Backed\\nby
    `node_authorize`" must still match once whitespace is normalized -- a
    per-physical-line regex silently drops this."""
    raw = "Invokes it. Backed\nby `node_authorize` in omnimarket -- logic lives in the node."
    normalized = re.sub(r"\s+", " ", raw)
    assert mod._BACKING_PHRASE_RE.search(normalized) is not None
