# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The architecture graph Bolt URI resolves from the per-host overlay row (OMN-24687).

Matches the projection read binding host overlay (OMN-23994): the variable
``ARCH_GRAPH_BOLT_URI`` keeps winning where a runtime sets it (the dev lane's containers),
and a host that sets nothing reads the row of its own short name from
``<overlay root>/arch_graph_bolt/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.handlers.arch_graph_bolt_host_overlay import (
    ARCH_GRAPH_BOLT_OVERLAY_ID,
    resolve_arch_graph_bolt_uri,
)
from omnimarket.nodes.node_architecture_graph_populate_effect.models import (
    ModelArchitectureGraphPopulateConfig,
)
from omnimarket.nodes.node_architecture_graph_query_effect.models import (
    ModelArchitectureGraphQueryConfig,
)

_URI = "bolt://graph.example.test:7687"
_OVERLAY = f"""\
schema_version: arch_graph_bolt.v1
bindings:
  - host: Mac-Host
    bolt_uri: {_URI}
  - host: lab-host-a
    bolt_uri: {_URI}
  - host: lab-host-b
    bolt_uri: {_URI}
"""


@pytest.fixture
def overlay_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
    target = tmp_path / ARCH_GRAPH_BOLT_OVERLAY_ID
    target.mkdir()
    (target / "overlay.yaml").write_text(_OVERLAY, encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    return tmp_path


@pytest.mark.unit
@pytest.mark.parametrize("host", ["lab-host-a", "LAB-HOST-B", "Mac-Host"])
def test_host_row_resolves_without_the_variable(overlay_root: Path, host: str) -> None:
    assert resolve_arch_graph_bolt_uri(host=host) == _URI


@pytest.mark.unit
def test_variable_wins_over_the_host_row(
    overlay_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARCH_GRAPH_BOLT_URI", "bolt://lane:7687")
    assert resolve_arch_graph_bolt_uri(host="lab-host-b") == "bolt://lane:7687"


@pytest.mark.unit
def test_host_without_a_row_fails_closed(overlay_root: Path) -> None:
    with pytest.raises(ValueError, match="ARCH_GRAPH_BOLT_URI is not bound"):
        resolve_arch_graph_bolt_uri(host="lab-host-z")


@pytest.mark.unit
def test_no_overlay_file_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(ValueError, match="ARCH_GRAPH_BOLT_URI is not bound"):
        resolve_arch_graph_bolt_uri(host="lab-host-b")


@pytest.mark.unit
def test_broken_overlay_is_refused_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
    target = tmp_path / ARCH_GRAPH_BOLT_OVERLAY_ID
    target.mkdir()
    (target / "overlay.yaml").write_text(
        "schema_version: arch_graph_bolt.v1\nbindings:\n  - {host: lab-host-b, bolt_uri: 'http://x'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(ValueError, match=ARCH_GRAPH_BOLT_OVERLAY_ID):
        resolve_arch_graph_bolt_uri(host="lab-host-b")


@pytest.mark.unit
def test_both_nodes_config_models_read_the_same_row(
    overlay_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnimarket.handlers.arch_graph_bolt_host_overlay.local_host_short_name",
        lambda: "lab-host-b",
    )
    assert ModelArchitectureGraphQueryConfig().bolt_uri == _URI
    assert ModelArchitectureGraphPopulateConfig().bolt_uri == _URI
