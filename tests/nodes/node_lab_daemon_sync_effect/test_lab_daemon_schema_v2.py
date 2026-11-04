# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Schema v2 adds portable surfaces without changing legacy daemon declarations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_daemon_sync_effect.__main__ import (
    DEFAULT_OVERLAY,
    load_overlay,
)
from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
    ModelDaemonOverlay,
    ModelSurfaceDeclaration,
    ModelTransactionalId,
)

pytestmark = pytest.mark.unit


def _document() -> dict[str, object]:
    raw: object = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return dict(raw)


def _surface(**overrides: object) -> dict[str, object]:
    return {
        "name": "example-plugin",
        "kind": "lane_tree",
        "applies_to_roles": ["lab", "lab-proof"],
        "repository": "example-repo",
        "repository_paths": ["plugins/example"],
        "runtime": "python-venv",
        "installed": {"kind": "json_file", "path": "${HOME}/x", "key": "commit"},
        **overrides,
    }


def test_schema_v2_superset() -> None:
    raw = _document()
    overlay = ModelDaemonOverlay.model_validate(raw)
    assert overlay.schema_version == 2
    legacy = {
        "schema_version": 1,
        "daemons": raw["daemons"],
        "fix_lane": raw["fix_lane"],
    }
    v1 = ModelDaemonOverlay.model_validate(legacy)
    assert [daemon.model_dump() for daemon in v1.daemons] == [
        daemon.model_dump() for daemon in overlay.daemons
    ]
    for key in ("clones", "chores", "fix_lane_repeat_hours"):
        legacy[key] = raw[key]
    complete_v1 = ModelDaemonOverlay.model_validate(legacy)
    for key in ("clones", "chores", "fix_lane_repeat_hours", "fix_lane"):
        assert complete_v1.model_dump()[key] == overlay.model_dump()[key]
    for key in ("surfaces", "layers", "transactional_ids"):
        with pytest.raises(ValidationError):
            ModelDaemonOverlay.model_validate({**legacy, key: raw[key]})
        with pytest.raises(ValidationError):
            ModelDaemonOverlay.model_validate(
                {k: v for k, v in raw.items() if k != key}
            )
    for key in ("surfaces", "transactional_ids"):
        with pytest.raises(ValidationError):
            ModelDaemonOverlay.model_validate({**raw, key: []})


@pytest.mark.parametrize(
    "overrides",
    [
        {"hosts": ["*"]},
        {"host": "example"},
        {"repository_paths": ["/Users/x/y"]},  # test-literal-ok
        {"repository_paths": ["plugins/../example"]},
        {"repository_paths": ["plugins/~example"]},
        {"installed": {"kind": "json_file", "path": "/opt/x"}},
        {"loaded": {"kind": "json_file", "path": "~/.config/revision"}},
        {"installed": {"kind": "command", "argv": ["git", "192.0.2.1"]}},
        {"loaded": {"kind": "command", "argv": ["git", "account@example.test"]}},
    ],
)
def test_no_host_literals(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelSurfaceDeclaration.model_validate(_surface(**overrides))
    accepted = ModelSurfaceDeclaration.model_validate(_surface())
    assert accepted.installed is not None
    assert accepted.installed.path == "${HOME}/x"


def test_layers_in_overlay() -> None:
    overlay = ModelDaemonOverlay.model_validate(_document())
    assert overlay.layers is not None
    assert overlay.layers.model_dump() == {"default_life_hours": 48, "max_life_days": 7}


def test_lane_tree_requires_runtime() -> None:
    raw = _surface()
    del raw["runtime"]
    with pytest.raises(ValidationError, match="runtime"):
        ModelSurfaceDeclaration.model_validate(raw)
    for kind in ("installed_plugin", "claude_config", "launchd_env", "observed"):
        with pytest.raises(ValidationError, match="runtime"):
            ModelSurfaceDeclaration.model_validate(_surface(kind=kind))


def test_launcher_refused_for_lane_tree() -> None:
    with pytest.raises(ValidationError, match="launcher"):
        ModelSurfaceDeclaration.model_validate(_surface(applies_to_roles=["launcher"]))


@pytest.mark.parametrize(
    ("identifier", "per_host", "valid"),
    [
        ("publisher", False, True),
        ("observed/{host}", True, True),
        ("observed", True, False),
        ("observed/{host}/{host}", True, False),
        ("observed/{host}", False, False),
        ("observed/{other}", False, False),
        ("observed/}", False, False),
    ],
)
def test_transactional_id_templates(
    identifier: str, per_host: bool, valid: bool
) -> None:
    raw = {"role": "publisher", "id": identifier, "per_host": per_host}
    if valid:
        assert ModelTransactionalId.model_validate(raw).id == identifier
    else:
        with pytest.raises(ValidationError):
            ModelTransactionalId.model_validate(raw)
    with pytest.raises(ValidationError):
        ModelTransactionalId.model_validate({**raw, "role": "Invalid Role"})


def test_surface_variables_named() -> None:
    surface = ModelSurfaceDeclaration.model_validate(
        _surface(
            description="Uses $DESCRIPTION and ${HOME}",
            repository="${SOURCE_REPO}",
            repository_paths=["${TREE}/plugins"],
            branch="$BRANCH",
            loaded={
                "kind": "command",
                "argv": ["${PYTHON}/bin/python", "$ARGS", "${HOME}/revision"],
                "key": "${RESULT}",
            },
        )
    )
    assert surface.variables() == (
        "ARGS",
        "BRANCH",
        "DESCRIPTION",
        "HOME",
        "PYTHON",
        "RESULT",
        "SOURCE_REPO",
        "TREE",
    )


@pytest.mark.parametrize("key", ["surfaces", "transactional_ids"])
def test_schema_v2_superset_unique_names_and_roles(key: str) -> None:
    raw = _document()
    entries = raw[key]
    assert isinstance(entries, list)
    with pytest.raises(ValidationError, match="unique"):
        ModelDaemonOverlay.model_validate({**raw, key: [*entries, entries[0]]})


@pytest.mark.parametrize(
    "overrides",
    [
        {"repository": None},
        {"repository": "group/repo"},
        {"repository": "${lowercase}"},
        {"installed": None},
        {"applies_to_roles": []},
        {"settings": [{"key": "autoUpdate", "value": False}]},
        {"env": [{"name": "EXAMPLE", "action": "unset"}]},
    ],
)
def test_surface_kind_constraints(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelSurfaceDeclaration.model_validate(_surface(**overrides))


@pytest.mark.parametrize("kind", ["claude_config", "launchd_env", "observed"])
def test_surface_kind_requires_its_payload(kind: str) -> None:
    raw = _surface(kind=kind)
    del raw["runtime"]
    with pytest.raises(ValidationError):
        ModelSurfaceDeclaration.model_validate(raw)


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "EXAMPLE", "action": "set"},
        {"name": "EXAMPLE", "action": "unset", "value": "value"},
        {"name": "bad-name", "action": "unset"},
    ],
)
def test_surface_env_action_values(entry: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelSurfaceDeclaration.model_validate(
            {
                "name": "environment",
                "kind": "launchd_env",
                "applies_to_roles": ["launcher"],
                "env": [entry],
            }
        )


def test_shipped_surfaces_and_loader_preserve_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_PLUGIN_ROOT", "plugin-root")
    overlay = load_overlay(DEFAULT_OVERLAY, [])
    raw = ModelDaemonOverlay.model_validate(_document())
    assert overlay.surfaces == raw.surfaces
    assert len(overlay.surfaces) == 13
    lane_trees = [
        surface for surface in overlay.surfaces if surface.kind == "lane_tree"
    ]
    assert len(lane_trees) == 4
    assert {surface.runtime for surface in lane_trees} == {"python-venv", "bash-hooks"}
    assert all("HOME" in surface.variables() for surface in lane_trees)


def test_schema_json_in_sync() -> None:
    path: Path = DEFAULT_OVERLAY.with_suffix(".schema.json")
    generated = json.dumps(
        ModelDaemonOverlay.model_json_schema(), indent=2, sort_keys=True
    )
    assert path.read_text(encoding="utf-8") == generated + "\n"
