# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one-copy check fails on the shape of the 2026-11-11 to 2026-11-14 evidence and passes on the corrected state (OMN-19998, ROOT 8).

The fixtures are excerpts shaped like the real lines, with the private package name replaced by a neutral one: a scheduled tick that
imports its own copy of the lab fill, and two writers of a host venv with their lock lines. The installed unit lab-fill-tick is read
from this repository's own host installer contract, so a rename on either side fails here (the unit-to-implementation seam). The
corrected state is the tick's text with the fill import removed, which is what the tick holds once its copy is deleted. The deployment's
own reading, over the real files, is in the deployment overlay store's change. Each failing example has its corrected control beside
it; each break is named.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_one_copy_check_compute.handlers.handler_one_copy_check import (
    FINDING_FIX_NOT_LIVE,
    FINDING_MULTIPLE_LIVE,
    FINDING_WRITER_WITHOUT_LOCK,
    decide_one_copy,
)
from omnimarket.nodes.node_one_copy_check_compute.models import (
    ModelOneCopyCheckOverlay,
    ModelOneCopyCheckResult,
    ModelReferrer,
    ModelReferrerSource,
)
from omnimarket.nodes.node_one_copy_check_compute.protocols.protocol_referrer_reader import (
    ReferrerReadError,
)
from omnimarket.nodes.node_one_copy_check_effect.protocols.protocol_file_referrer_reader import (
    FileReferrerReader,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parents[5]
AS_OF = datetime(2026, 11, 15, 0, 40, tzinfo=UTC)
LAB_FILL_MARKER = "omnimarket.nodes.node_lab_fill_effect"
TICK_FILL_MARKER = "legacy_pkg.nodes.node_idle_slot_fill_effect"
UNIT_SOURCE = ModelReferrerSource(
    id="unit:lab-fill-tick",
    kind="unit",
    path="src/omnimarket/nodes/node_host_installer_registry_units/contract.yaml",
    yaml_key="units.lab-fill-tick",
)
OVERLAY = ModelOneCopyCheckOverlay.model_validate(
    {
        "emission_piece": 10,
        "behaviours": [
            {
                "name": "lab_fill",
                "fix_landed_in": "market_lab_fill",
                "implementations": [
                    {"id": "tick_idle_slot_fill", "markers": [TICK_FILL_MARKER]},
                    {
                        "id": "market_idle_slot_fill",
                        "markers": ["omnimarket.nodes.node_idle_slot_fill_effect"],
                    },
                    {"id": "market_lab_fill", "markers": [LAB_FILL_MARKER]},
                ],
            }
        ],
        "resources": [
            {
                "name": "host_venv",
                "write_markers": ["uv pip install", "uv sync", "uv venv"],
                "lock_markers": [
                    "reconcile-workspace-venvs.sh",
                    "staging-lock-acquire",
                    "ONEX_PLUGIN_VENV_LOCK_FD",
                ],
                "comment_prefixes": ["#"],
            }
        ],
        "referrer_sources": [UNIT_SOURCE.model_dump()],
    }
)


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _tick(name: str) -> ModelReferrer:
    return ModelReferrer(id="tick:throughput-tick", kind="tick", text=_fixture(name))


def _unit() -> ModelReferrer:
    (unit,) = FileReferrerReader(REPO_ROOT, [UNIT_SOURCE]).read()
    return unit


def _codes(result: ModelOneCopyCheckResult) -> list[str]:
    return sorted(finding.code for finding in result.findings)


# ------------------------------------------------------------------------------------------ the lab fill, three states


def test_before_the_unit_the_fix_landed_in_a_copy_no_unit_ran_and_the_check_fails() -> (
    None
):
    # 2026-11-14: omnimarket 6126 landed in the lab fill nodes; the only thing running a fill was the tick's own copy.
    result = decide_one_copy(
        OVERLAY, [_tick("tick_excerpt_with_fill_import.py.txt")], AS_OF
    )

    assert not result.ok
    assert _codes(result) == [FINDING_FIX_NOT_LIVE]
    assert [item.id for item in result.behaviours[0].live] == ["tick_idle_slot_fill"]


def test_with_the_unit_declared_and_the_tick_still_importing_its_copy_two_copies_are_live_and_the_check_fails() -> (
    None
):
    result = decide_one_copy(
        OVERLAY, [_tick("tick_excerpt_with_fill_import.py.txt"), _unit()], AS_OF
    )

    assert not result.ok
    assert _codes(result) == [FINDING_MULTIPLE_LIVE]
    assert {item.id for item in result.behaviours[0].live} == {
        "tick_idle_slot_fill",
        "market_lab_fill",
    }
    assert result.behaviours[0].unreferenced == ("market_idle_slot_fill",)


def test_corrected_the_tick_no_longer_imports_its_copy_and_one_copy_is_live_the_one_the_fix_landed_in() -> (
    None
):
    result = decide_one_copy(
        OVERLAY, [_tick("tick_excerpt_fill_import_removed.py.txt"), _unit()], AS_OF
    )

    assert result.ok, result.findings
    assert [item.id for item in result.behaviours[0].live] == ["market_lab_fill"]
    assert result.behaviours[0].live[0].referrers == ("unit:lab-fill-tick",)


# --------------------------------------------------------------------------------- seam: the unit to its implementation


def test_seam_the_installed_unit_names_the_lab_fill_implementation_the_overlay_marker_names() -> (
    None
):
    # Fails when the unit is renamed or its module moves (the unit side), or the marker no longer names a module (the implementation side).
    assert LAB_FILL_MARKER in _unit().text
    assert importlib.util.find_spec(LAB_FILL_MARKER) is not None


def test_seam_a_unit_key_that_is_missing_is_a_typed_read_failure_not_a_clean_check() -> (
    None
):
    renamed = UNIT_SOURCE.model_copy(update={"yaml_key": "units.lab-fill-tick-renamed"})
    with pytest.raises(ReferrerReadError):
        FileReferrerReader(REPO_ROOT, [renamed]).read()


# ------------------------------------------------------------------------------------- the venv and its writers


@pytest.mark.parametrize(
    ("fixture", "lock_marker"),
    [
        ("writer_reconcile_excerpt.sh.txt", "staging-lock-acquire"),
        ("writer_plugin_venv_excerpt.sh.txt", "ONEX_PLUGIN_VENV_LOCK_FD"),
    ],
)
def test_a_venv_writer_that_takes_its_lock_passes_and_the_same_writer_with_the_lock_removed_fails(
    fixture: str, lock_marker: str
) -> None:
    text = _fixture(fixture)
    writer = ModelReferrer(id=f"writer:{fixture}", kind="source", text=text)
    # Positive control: the writer really writes (the excerpt holds a non-comment write line), so the pass below is not vacuous.
    assert any(
        marker in line and not line.lstrip().startswith("#")
        for line in text.splitlines()
        for marker in OVERLAY.resources[0].write_markers
    )

    corrected = decide_one_copy(OVERLAY, [writer], AS_OF)
    assert corrected.resources[0].writers == (writer.id,)
    assert corrected.resources[0].writers_without_lock == ()

    # Named injected break: the lock line is removed from the writer's text.
    broken = writer.model_copy(
        update={"text": text.replace(lock_marker, "removed-lock-line")}
    )
    result = decide_one_copy(OVERLAY, [broken], AS_OF)
    assert FINDING_WRITER_WITHOUT_LOCK in _codes(result)
    assert result.resources[0].writers_without_lock == (writer.id,)
