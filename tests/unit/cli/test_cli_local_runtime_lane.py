# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex local init`` declares this machine's runtime lane (OMN-19750).

Runtime lane overlays plan, task LO6. A runtime learns which lane it is only from
a ``runtime.lane`` overlay document that whoever runs it supplies, read from the
one overlay source its deployment selects. On a local install that source is
local-home, and nobody should have to hand-write either file. So init writes:

* ``~/.onex/config.yaml`` ``config_source: local-home``, the bootstrap fact that
  selects the source (every other key in that file is kept), and
* the shipped example ``runtime.lane`` document, byte for byte, to
  ``~/.omninode/config/<environment>/<lane>/runtime.lane.json`` at mode 0600.

A second run changes nothing. A different document already at that path, or a
bootstrap file that selects another source, is refused and left as it was: init
never overwrites an operator's own declaration.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner, Result
from omnibase_core.enums.enum_config_overlay_key import EnumConfigOverlayKey
from omnibase_core.enums.enum_config_overlay_source import EnumConfigOverlaySource
from omnibase_core.models.config_overlay import (
    ModelConfigOverlayDocument,
    ModelRuntimeLaneDeclaration,
)
from omnibase_infra.examples.config_overlays import read_runtime_lane_example

from omnimarket.cli.cli_local import local_group
from omnimarket.local_deployment.runtime_lane import (
    LOCAL_OVERLAY_ENVIRONMENT,
    local_runtime_lane_path,
)
from omnimarket.local_deployment.tenant_identity import (
    reset_local_tenant_identity_cache,
)

pytestmark = pytest.mark.unit

_EXAMPLE = read_runtime_lane_example()
_EXAMPLE_LANE = str(json.loads(_EXAMPLE)["lane_id"])


@pytest.fixture(autouse=True)
def _fresh_identity_cache() -> None:
    reset_local_tenant_identity_cache()


def _init(home: Path, *args: str) -> Result:
    return CliRunner().invoke(
        local_group,
        ["init", "--store", str(home / "store" / "delegation.sqlite"), *args],
        env={"HOME": str(home)},
    )


def _document_path(home: Path) -> Path:
    return (
        home
        / ".omninode"
        / "config"
        / LOCAL_OVERLAY_ENVIRONMENT
        / _EXAMPLE_LANE
        / f"{EnumConfigOverlayKey.RUNTIME_LANE.value}.json"
    )


def _bootstrap(home: Path) -> dict[str, object]:
    loaded = yaml.safe_load((home / ".onex" / "config.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_path_is_scope_keyed_under_local_home(tmp_path: Path) -> None:
    assert local_runtime_lane_path(home=tmp_path, lane_id=_EXAMPLE_LANE) == (
        _document_path(tmp_path)
    )


def test_init_on_an_empty_home_writes_the_shipped_example(tmp_path: Path) -> None:
    result = _init(tmp_path)
    assert result.exit_code == 0, result.output
    path = _document_path(tmp_path)
    assert path.read_bytes() == _EXAMPLE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "written" in result.output
    assert hashlib.sha256(_EXAMPLE).hexdigest() in result.output


def test_init_selects_the_local_home_source(tmp_path: Path) -> None:
    assert _init(tmp_path).exit_code == 0
    assert _bootstrap(tmp_path)["config_source"] == "local-home"


def test_the_written_document_resolves_as_a_runtime_would(tmp_path: Path) -> None:
    assert _init(tmp_path).exit_code == 0
    raw = _document_path(tmp_path).read_bytes()
    key = EnumConfigOverlayKey.RUNTIME_LANE
    content = json.loads(raw)
    document = ModelConfigOverlayDocument(
        key=key,
        schema_ref=key.schema_ref,
        schema_version=content["schema_version"],
        source=EnumConfigOverlaySource.LOCAL_HOME,
        sha256=hashlib.sha256(raw).hexdigest(),
        content=content,
    )
    declaration = ModelRuntimeLaneDeclaration.resolve(
        declared_lane_id=_EXAMPLE_LANE, document=document, where="local-home"
    )
    assert declaration.lane_id == _EXAMPLE_LANE


def test_a_second_init_changes_nothing(tmp_path: Path) -> None:
    assert _init(tmp_path).exit_code == 0
    path = _document_path(tmp_path)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    second = _init(tmp_path)
    assert second.exit_code == 0, second.output
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert "already present" in second.output


def test_init_refuses_to_overwrite_a_different_declaration(tmp_path: Path) -> None:
    path = _document_path(tmp_path)
    path.parent.mkdir(parents=True)
    own = b'{"schema_version": "runtime_lane.v1", "lane_id": "local", "roles": ["lab"], "description": "mine"}'
    path.write_bytes(own)
    path.chmod(0o600)
    result = _init(tmp_path)
    assert result.exit_code != 0
    assert str(path) in result.output
    assert path.read_bytes() == own


def test_init_keeps_every_other_bootstrap_key(tmp_path: Path) -> None:
    onex = tmp_path / ".onex"
    onex.mkdir()
    (onex / "config.yaml").write_text(yaml.safe_dump({"other_writer": {"k": "v"}}))
    assert _init(tmp_path).exit_code == 0
    assert _bootstrap(tmp_path) == {
        "other_writer": {"k": "v"},
        "config_source": "local-home",
    }


def test_init_refuses_a_bootstrap_file_that_selects_another_source(
    tmp_path: Path,
) -> None:
    onex = tmp_path / ".onex"
    onex.mkdir()
    (onex / "config.yaml").write_text(yaml.safe_dump({"config_source": "store"}))
    result = _init(tmp_path)
    assert result.exit_code != 0
    assert "store" in result.output
    assert _bootstrap(tmp_path) == {"config_source": "store"}
    assert not _document_path(tmp_path).exists()


def test_json_output_reports_the_lane_document(tmp_path: Path) -> None:
    result = _init(tmp_path, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    lane = payload["runtime_lane"]
    assert lane["lane_id"] == _EXAMPLE_LANE
    assert lane["path"] == str(_document_path(tmp_path))
    assert lane["sha256"] == hashlib.sha256(_EXAMPLE).hexdigest()
    assert lane["newly_written"] is True
