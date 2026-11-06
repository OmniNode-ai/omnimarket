# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The model the overlay home validates the ``runtime_lane_env`` overlay through (OMN-22632).

Every lane id, name and value here is synthetic: the real document is private deployment data and
stays in the overlay home.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.node_overlays.model_runtime_lane_env_overlay import (
    ModelRuntimeLaneEnvOverlay,
)

pytestmark = pytest.mark.unit

LANE = "compose-lane-a"
ENV = {
    "SOURCE_REPOSITORY": "web-front-end",
    "SOURCE_URL": "https://example.invalid/web-front-end.git",
}


def _document(**overrides: object) -> dict[str, object]:
    document: dict[str, object] = {
        "schema_version": "runtime_lane_env.v1",
        "lanes": {LANE: {"description": "synthetic", "env": dict(ENV)}},
    }
    document.update(overrides)
    return document


def test_golden_loads_the_way_the_overlay_home_loads_it(tmp_path: Path) -> None:
    (tmp_path / "runtime_lane_env").mkdir()
    (tmp_path / "runtime_lane_env" / "overlay.yaml").write_text(
        yaml.safe_dump(_document()), encoding="utf-8"
    )
    loaded = load_structured_overlay(
        "runtime_lane_env",
        ModelRuntimeLaneEnvOverlay,
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
    )
    assert loaded.lane_env(LANE) == ENV
    assert loaded.lane_env("compose-lane-z") == {}


def test_golden_two_lanes_keep_their_own_env() -> None:
    other = {"SOURCE_REPOSITORY": "other"}
    loaded = ModelRuntimeLaneEnvOverlay.model_validate(
        _document(lanes={LANE: {"env": ENV}, "compose-lane-b": {"env": other}})
    )
    assert loaded.lane_env(LANE) == ENV
    assert loaded.lane_env("compose-lane-b") == other


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (_document(stray="x"), "stray"),
        (_document(schema_version="runtime_lane_env.v0"), "schema_version"),
        (_document(lanes={}), "lanes"),
        (_document(lanes={"": {"env": ENV}}), "lanes"),
        (_document(lanes={LANE: {"env": ENV, "stray": "x"}}), "stray"),
        (_document(lanes={LANE: {"env": {}}}), "env"),
        (_document(lanes={LANE: {"env": {"bad name": "x"}}}), "env"),
        (_document(lanes={LANE: {"env": {"SOURCE_URL": ""}}}), "SOURCE_URL"),
        (_document(lanes={LANE: {"env": {"SOURCE_URL": "  "}}}), "SOURCE_URL"),
        (_document(lanes={LANE: {"env": {"SOURCE_URL": "a\nb"}}}), "SOURCE_URL"),
        (_document(lanes={LANE: {"env": {"SOURCE_URL": False}}}), "SOURCE_URL"),
    ],
)
def test_error_a_malformed_document_refuses_by_field(
    document: dict[str, object], field: str
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        ModelRuntimeLaneEnvOverlay.model_validate(document)
    assert field in str(excinfo.value)


def test_error_a_missing_schema_version_refuses() -> None:
    document = _document()
    del document["schema_version"]
    with pytest.raises(ValidationError, match="schema_version"):
        ModelRuntimeLaneEnvOverlay.model_validate(document)


PROBE = (
    "import sys\n"
    "{extra}"
    "import omnimarket.models.node_overlays.model_runtime_lane_env_overlay\n"
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
