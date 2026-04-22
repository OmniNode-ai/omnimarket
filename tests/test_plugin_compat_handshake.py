# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for omnimarket.runtime.version_handshake (OMN-8789).

Tests written first (failing), then implementation added.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.runtime.version_handshake import (
    ModelPluginCompatResult,
    check_plugin_compat,
    load_compat_matrix,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def compat_yaml_content() -> dict:
    return {
        "plugin": "onex",
        "plugin_version": "0.23.0",
        "min_runtime_version": "0.2.0",
        "max_runtime_version": "1.0.0",
        "nodes": [
            {
                "node": "node_aislop_sweep",
                "topics": [
                    {
                        "name": "onex.cmd.omnimarket.aislop-sweep-start.v1",
                        "schema_version": "v1",
                        "role": "subscribe",
                    },
                    {
                        "name": "onex.evt.omnimarket.aislop-sweep-completed.v1",
                        "schema_version": "v1",
                        "role": "publish",
                    },
                ],
            },
            {
                "node": "node_merge_sweep_compute",
                "topics": [
                    {
                        "name": "onex.cmd.omnimarket.merge-sweep-start.v1",
                        "schema_version": "v1",
                        "role": "subscribe",
                    },
                ],
            },
        ],
    }


@pytest.fixture
def compat_yaml_file(tmp_path: Path, compat_yaml_content: dict) -> Path:
    p = tmp_path / "plugin-compat.yaml"
    p.write_text(yaml.dump(compat_yaml_content))
    return p


# ---------------------------------------------------------------------------
# load_compat_matrix
# ---------------------------------------------------------------------------


class TestLoadCompatMatrix:
    def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        result = load_compat_matrix(tmp_path / "nonexistent.yaml")
        assert result is None

    def test_parses_valid_yaml(self, compat_yaml_file: Path) -> None:
        matrix = load_compat_matrix(compat_yaml_file)
        assert matrix is not None
        assert matrix.plugin == "onex"
        assert matrix.plugin_version == "0.23.0"
        assert matrix.min_runtime_version == "0.2.0"
        assert matrix.max_runtime_version == "1.0.0"

    def test_parses_nodes(self, compat_yaml_file: Path) -> None:
        matrix = load_compat_matrix(compat_yaml_file)
        assert matrix is not None
        assert len(matrix.nodes) == 2
        assert matrix.nodes[0].node == "node_aislop_sweep"
        assert len(matrix.nodes[0].topics) == 2

    def test_parses_topic_fields(self, compat_yaml_file: Path) -> None:
        matrix = load_compat_matrix(compat_yaml_file)
        assert matrix is not None
        topic = matrix.nodes[0].topics[0]
        assert topic.name == "onex.cmd.omnimarket.aislop-sweep-start.v1"
        assert topic.schema_version == "v1"
        assert topic.role == "subscribe"


# ---------------------------------------------------------------------------
# check_plugin_compat — compatible cases
# ---------------------------------------------------------------------------


class TestCheckPluginCompatCompatible:
    def test_compatible_version_within_window(self, compat_yaml_file: Path) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="0.5.0",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is True
        assert result.runtime_version == "0.5.0"
        assert result.warnings == []

    def test_compatible_at_min_version(self, compat_yaml_file: Path) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="0.2.0",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is True

    def test_skips_gracefully_when_file_absent(self, tmp_path: Path) -> None:
        result = check_plugin_compat(compat_path=tmp_path / "missing.yaml")
        assert result.compatible is True
        assert result.skip_reason is not None

    def test_skips_gracefully_when_package_not_installed(
        self, compat_yaml_file: Path
    ) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value=None,
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is True
        assert result.skip_reason is not None


# ---------------------------------------------------------------------------
# check_plugin_compat — mismatch cases
# ---------------------------------------------------------------------------


class TestCheckPluginCompatMismatch:
    def test_fails_when_runtime_below_min(self, compat_yaml_file: Path) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="0.1.9",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is False
        assert len(result.warnings) == 1
        assert "0.1.9" in result.warnings[0]
        assert "0.2.0" in result.warnings[0]

    def test_fails_when_runtime_at_max(self, compat_yaml_file: Path) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="1.0.0",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is False
        assert len(result.warnings) == 1
        assert "1.0.0" in result.warnings[0]

    def test_fails_when_runtime_above_max(self, compat_yaml_file: Path) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="2.0.0",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.compatible is False
        assert result.warnings

    def test_result_contains_version_fields_on_mismatch(
        self, compat_yaml_file: Path
    ) -> None:
        with patch(
            "omnimarket.runtime.version_handshake._installed_version",
            return_value="0.1.0",
        ):
            result = check_plugin_compat(compat_path=compat_yaml_file)

        assert result.plugin_version == "0.23.0"
        assert result.runtime_version == "0.1.0"
        assert result.min_runtime_version == "0.2.0"
        assert result.max_runtime_version == "1.0.0"


# ---------------------------------------------------------------------------
# ModelPluginCompatResult immutability
# ---------------------------------------------------------------------------


class TestModelPluginCompatResult:
    def test_is_frozen(self) -> None:
        result = ModelPluginCompatResult(compatible=True)
        with pytest.raises(ValidationError):
            result.compatible = False  # type: ignore[misc]

    def test_defaults(self) -> None:
        result = ModelPluginCompatResult(compatible=True)
        assert result.warnings == []
        assert result.skip_reason is None
