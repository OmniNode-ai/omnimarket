# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model the overlay home validates the ``lane_time_box`` overlay through (OMN-23999).

Every host here is synthetic: the real document is private deployment data and stays in the
overlay home.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.node_overlays.model_lane_time_box_overlay import (
    MAX_TIMEOUT_MIN,
    ModelLaneTimeBoxOverlay,
)
from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as guard,
)

pytestmark = pytest.mark.unit

HOST = "h053"
OTHER = "h201"
DEFAULT_TIMEOUT_MIN = 178


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": 1,
        "default_timeout_min": DEFAULT_TIMEOUT_MIN,
        "hosts": {},
    }
    document.update(overrides)
    return document


def test_golden_an_empty_overlay_loads_and_every_host_takes_the_default() -> None:
    loaded = ModelLaneTimeBoxOverlay.model_validate(_document())
    assert loaded.schema_version == 1
    assert loaded.default_timeout_min == DEFAULT_TIMEOUT_MIN
    assert loaded.timeout_for(None) == DEFAULT_TIMEOUT_MIN
    assert loaded.timeout_for(HOST) == DEFAULT_TIMEOUT_MIN


def test_golden_a_host_row_overrides_the_default() -> None:
    loaded = ModelLaneTimeBoxOverlay.model_validate(
        _document(hosts={HOST: {"timeout_min": 150}})
    )
    assert loaded.timeout_for(HOST) == 150
    assert loaded.timeout_for(OTHER) == DEFAULT_TIMEOUT_MIN


def test_golden_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    (tmp_path / "lane_time_box").mkdir()
    (tmp_path / "lane_time_box" / "overlay.yaml").write_text(
        "# comment\nschema_version: 1\ndefault_timeout_min: 178  # minutes\nhosts: {}\n",
        encoding="utf-8",
    )
    loaded = load_structured_overlay(
        "lane_time_box",
        ModelLaneTimeBoxOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert loaded.default_timeout_min == DEFAULT_TIMEOUT_MIN
    assert loaded.timeout_for(None) == DEFAULT_TIMEOUT_MIN
    assert loaded.timeout_for(HOST) == DEFAULT_TIMEOUT_MIN


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (_document(default_timeout_min=180), "default_timeout_min"),
        (_document(default_timeout_min=0), "default_timeout_min"),
        (_document(default_timeout_min=True), "default_timeout_min"),
        (_document(default_timeout_min="90"), "default_timeout_min"),
        (_document(default_timeout_min=90.5), "default_timeout_min"),
        (_document(hosts={HOST: {"timeout_min": 999}}), "timeout_min"),
        (_document(stray="x"), "stray"),
        (_document(schema_version=2), "schema_version"),
        (_document(hosts={HOST: {"timeout_min": 150, "stray": "x"}}), "stray"),
    ],
)
def test_error_a_malformed_document_refuses_by_field(
    document: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelLaneTimeBoxOverlay.model_validate(document)
    assert field in str(excinfo.value)


def test_golden_the_ceiling_is_the_lane_guard_s_cap_less_its_grace() -> None:
    assert MAX_TIMEOUT_MIN == 178
    assert MAX_TIMEOUT_MIN == (guard.HARD_CAP_S - guard.GRACE_S) // 60


PROBE = (
    "import sys\n"
    "{extra}"
    "import omnimarket.models.node_overlays.model_lane_time_box_overlay\n"
    "allowed = set(sys.stdlib_module_names) | {{'pydantic', 'pydantic_core', 'annotated_types', 'typing_extensions', 'omnimarket', '_distutils_hack', 'typing_inspection'}}\n"
    "heavy = sorted({{n.split('.')[0] for n in sys.modules}} - allowed - {{n for n in sys.modules if n.startswith('_')}})\n"
    "assert not heavy, heavy\n"
)


def _import_probe(extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", PROBE.format(extra=extra)],
        capture_output=True,
        text=True,
        check=False,
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
    )


def test_golden_the_model_imports_with_pydantic_alone() -> None:
    result = _import_probe("")
    assert result.returncode == 0, result.stderr


def test_control_an_extra_import_turns_the_import_check_red() -> None:
    """The failing control: the same probe with yaml imported first must refuse."""
    result = _import_probe("import yaml\n")
    assert result.returncode != 0
    assert "yaml" in result.stderr
