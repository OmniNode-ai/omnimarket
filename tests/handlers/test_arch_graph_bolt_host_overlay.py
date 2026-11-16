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
import yaml

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
bolt_uri: {_URI}
hosts:
  - Mac-Host
  - lab-host-a
  - lab-host-b
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
def test_arch_graph_bolt_overlay_declares_the_uri_once_for_a_hosts_list() -> None:
    from omnimarket.models.node_overlays.model_arch_graph_bolt_overlay import (
        ModelArchGraphBoltOverlay,
    )

    overlay = ModelArchGraphBoltOverlay.model_validate(yaml.safe_load(_OVERLAY))
    assert overlay.bolt_uri == _URI
    assert overlay.hosts == ("Mac-Host", "lab-host-a", "lab-host-b")


@pytest.mark.unit
def test_arch_graph_bolt_overlay_refuses_per_host_rows_and_duplicate_hosts() -> None:
    from pydantic import ValidationError

    from omnimarket.models.node_overlays.model_arch_graph_bolt_overlay import (
        ModelArchGraphBoltOverlay,
    )

    with pytest.raises(ValidationError):
        ModelArchGraphBoltOverlay.model_validate(
            {
                "schema_version": "arch_graph_bolt.v1",
                "bindings": [{"host": "h", "bolt_uri": _URI}],
            }
        )
    with pytest.raises(ValidationError):
        ModelArchGraphBoltOverlay.model_validate(
            {
                "schema_version": "arch_graph_bolt.v1",
                "bolt_uri": _URI,
                "hosts": ["a", "A"],
            }
        )


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
        "schema_version: arch_graph_bolt.v1\nbolt_uri: 'http://x'\nhosts: [lab-host-b]\n",
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


@pytest.mark.unit
def test_arch_graph_bolt_handler_imports_the_one_short_host_name_helper() -> None:
    from omnimarket.handlers import arch_graph_bolt_host_overlay as mod
    from omnimarket.handlers.projection_read_binding_host_overlay import (
        local_host_short_name,
    )

    assert mod.local_host_short_name is local_host_short_name


@pytest.mark.unit
def test_arch_graph_bolt_dotted_gethostname_resolves_the_short_name_row(
    overlay_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnimarket.handlers.projection_read_binding_host_overlay.socket.gethostname",
        lambda: "lab-host-b.example.test",
    )
    assert resolve_arch_graph_bolt_uri() == _URI


@pytest.mark.unit
def test_arch_graph_bolt_broken_overlay_refusal_carries_the_reader_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ARCH_GRAPH_BOLT_URI", raising=False)
    target = tmp_path / ARCH_GRAPH_BOLT_OVERLAY_ID
    target.mkdir()
    (target / "overlay.yaml").write_text(
        "schema_version: arch_graph_bolt.v1\nbolt_uri: 'http://x'\nhosts: [lab-host-b]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    # the reader's own message names the roots variable and the invalid field
    with pytest.raises(ValueError, match=r"ONEX_SKILL_OVERLAY_ROOTS.*bolt_uri"):
        resolve_arch_graph_bolt_uri(host="lab-host-b")
