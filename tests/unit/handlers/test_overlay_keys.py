# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The 21 private-value overlay keys: every key resolves from a fixture and refuses when nothing supplies it."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.handlers.overlay_keys import (
    LAB_HOSTS_FILE_ENV,
    OVERLAY_DOCUMENT_MODELS,
    OVERLAY_KEYS,
    REGISTRY_ROOT_ENV,
    resolve_clone_home,
    resolve_overlay_key,
)

pytestmark = pytest.mark.unit

_HOST_ROW = {"name": "host-a", "target": "192.0.2.10", "local": True, "penalty": 0.1}

# Neutral values: documentation-range addresses and placeholder names only.
FIXTURE_DOCUMENTS: dict[str, dict[str, Any]] = {
    "repos": {
        "plugin_source": "repo-plugin-source",
        "private_data": "repo-private-data",
        "knowledge_base": "repo-knowledge-base",
        "deploy": "repo-deploy",
        "private_data_home_envs": [
            "EXAMPLE_PRIVATE_DATA_HOME",
            "EXAMPLE_PRIVATE_DATA_PATH",
        ],
        "knowledge_base_home_env": "EXAMPLE_KNOWLEDGE_BASE_PATH",
        "beside_root": ["private_data", "knowledge_base"],
    },
    "lab_hosts": {
        "hosts": [_HOST_ROW, {"name": "host-b", "target": "192.0.2.11"}],
        "registry_root_candidates": ["/srv/example/registry", "$HOME/registry"],
        "bus_lane": "example-lane",
    },
    "hygiene_vocab": {
        "vocabulary_rel": "docs/example_vocabulary.yaml",
        "lab_vocabulary_rel": "vocabularies/example_lab_vocabulary.yaml",
    },
    "remote_lane": {
        "runtime_config_rel": "config/example/runtime_config.yaml",
        "cloud_excluded_roles": ["deploy", "knowledge_base"],
    },
    "idle_slot_fill": {
        "process_fix_roles": ["private_data", "plugin_source"],
        "mvp_repos": ["repo-one", "repo-two"],
        "always_out_roles": ["deploy"],
        "cloud_backlog": {
            "ruling": "Dated paraphrase: the operator allowed backlog tickets to go to the cloud.",
            "note": "What a free cloud slot may take from the backlog.",
            "repos": ["repo-one"],
            "excluded_repos": ["repo-deploy"],
            "lab_markers": ["lab host", "host-a"],
            "sprint_state_types": ["unstarted", "backlog"],
            "max_estimate": 3,
            "window_hours": 24,
        },
        "experiment_designs": [
            {
                "id": "example-design",
                "title": "Example",
                "repo": "repo-one",
                "design": "Run the same task twice.",
                "measure": "Record the count of passes.",
            }
        ],
    },
    "throughput_tick": {
        "host_tick_fix": "reinstall it through the host installer",
        "host_tick_owned_fix": "its own installer owns it: run {owner} for {name}, then read {record}",
        "host_ticks_fix": "set the variable that names the private data clone",
    },
}

# What each key resolves to from the fixture, as plain data.
EXPECTED: dict[str, Any] = {
    f"{overlay_id}.{field}": value
    for overlay_id, document in FIXTURE_DOCUMENTS.items()
    for field, value in document.items()
    if field != "bus_lane"
}


def _write_overlays(root: Path, documents: Mapping[str, Mapping[str, Any]]) -> None:
    for overlay_id, document in documents.items():
        path = root / overlay_id / "overlay.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(dict(document)), encoding="utf-8")


def _plain(value: Any) -> Any:
    """Reduce a resolved value (models, tuples) to the plain data the fixture wrote."""
    if hasattr(value, "model_dump"):
        return _plain(value.model_dump(exclude_unset=True))
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


@pytest.fixture
def overlay_root(tmp_path: Path) -> Path:
    root = tmp_path / "overlays"
    _write_overlays(root, FIXTURE_DOCUMENTS)
    return root


def _env(root: Path | None, **extra: str) -> dict[str, str]:
    env = dict(extra)
    if root is not None:
        env[OVERLAY_ROOTS_ENV] = str(root)
    return env


ALL_KEYS = [spec.key for spec in OVERLAY_KEYS]


def test_overlay_keys_are_the_21_of_the_inventory() -> None:
    assert len(ALL_KEYS) == 21
    assert len(set(ALL_KEYS)) == 21
    assert sorted(OVERLAY_DOCUMENT_MODELS) == [
        "hygiene_vocab",
        "idle_slot_fill",
        "lab_hosts",
        "remote_lane",
        "repos",
        "throughput_tick",
    ]
    assert set(ALL_KEYS) == set(EXPECTED)


@pytest.mark.parametrize("overlay_id", sorted(OVERLAY_DOCUMENT_MODELS))
def test_overlay_keys_fixture_file_validates_and_holds_only_its_keys(
    overlay_root: Path, overlay_id: str
) -> None:
    raw = yaml.safe_load(
        (overlay_root / overlay_id / "overlay.yaml").read_text(encoding="utf-8")
    )
    model = OVERLAY_DOCUMENT_MODELS[overlay_id]
    model.model_validate(raw)
    keys = {f"{overlay_id}.{field}" for field in raw} - {"lab_hosts.bus_lane"}
    assert keys == {key for key in ALL_KEYS if key.startswith(f"{overlay_id}.")}


@pytest.mark.parametrize("key", ALL_KEYS)
def test_overlay_keys_resolve_from_fixture(overlay_root: Path, key: str) -> None:
    value = resolve_overlay_key(key, environ=_env(overlay_root))
    assert _plain(value) == EXPECTED[key]


@pytest.mark.parametrize("key", ALL_KEYS)
def test_overlay_keys_refuse_naming_the_key_when_nothing_supplies_it(
    tmp_path: Path, key: str
) -> None:
    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    with pytest.raises(NodeOverlayError, match=key.replace(".", r"\.")):
        resolve_overlay_key(key, environ=_env(empty_root))
    with pytest.raises(NodeOverlayError, match=key.replace(".", r"\.")):
        resolve_overlay_key(key, environ={})


def test_overlay_keys_refusal_names_the_variables_that_supply_the_key(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=LAB_HOSTS_FILE_ENV):
        resolve_overlay_key("lab_hosts.hosts", environ=_env(tmp_path))
    with pytest.raises(NodeOverlayError, match=REGISTRY_ROOT_ENV):
        resolve_overlay_key(
            "lab_hosts.registry_root_candidates", environ=_env(tmp_path)
        )


def test_overlay_keys_unknown_key_is_refused() -> None:
    with pytest.raises(NodeOverlayError, match=r"repos\.nothing"):
        resolve_overlay_key("repos.nothing", environ={})


def test_overlay_keys_hosts_precedence_explicit_then_file_then_overlay(
    overlay_root: Path, tmp_path: Path
) -> None:
    table = tmp_path / "table.yaml"
    table.write_text(
        yaml.safe_dump({"hosts": [{"name": "host-file", "target": "192.0.2.20"}]}),
        encoding="utf-8",
    )
    env = _env(overlay_root, **{LAB_HOSTS_FILE_ENV: str(table)})

    explicit = [{"name": "host-explicit", "target": "192.0.2.30"}]
    assert [
        h.name
        for h in resolve_overlay_key("lab_hosts.hosts", explicit=explicit, environ=env)
    ] == ["host-explicit"]
    assert [h.name for h in resolve_overlay_key("lab_hosts.hosts", environ=env)] == [
        "host-file"
    ]
    env_without_file = _env(overlay_root)
    assert [
        h.name for h in resolve_overlay_key("lab_hosts.hosts", environ=env_without_file)
    ] == ["host-a", "host-b"]


def test_overlay_keys_hosts_file_that_is_missing_or_invalid_is_refused_not_skipped(
    overlay_root: Path, tmp_path: Path
) -> None:
    missing = _env(overlay_root, **{LAB_HOSTS_FILE_ENV: str(tmp_path / "absent.yaml")})
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.hosts"):
        resolve_overlay_key("lab_hosts.hosts", environ=missing)
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"hosts": [{"name": "host-a"}]}), encoding="utf-8")
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.hosts"):
        resolve_overlay_key(
            "lab_hosts.hosts",
            environ=_env(overlay_root, **{LAB_HOSTS_FILE_ENV: str(bad)}),
        )


def test_overlay_keys_registry_roots_precedence_variable_then_overlay(
    overlay_root: Path,
) -> None:
    pinned = _env(overlay_root, **{REGISTRY_ROOT_ENV: "/srv/pinned"})
    assert resolve_overlay_key(
        "lab_hosts.registry_root_candidates", environ=pinned
    ) == ("/srv/pinned",)
    assert resolve_overlay_key(
        "lab_hosts.registry_root_candidates", environ=_env(overlay_root)
    ) == ("/srv/example/registry", "$HOME/registry")
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.registry_root_candidates"):
        resolve_overlay_key(
            "lab_hosts.registry_root_candidates",
            environ=_env(overlay_root, **{REGISTRY_ROOT_ENV: "has space"}),
        )


def test_overlay_keys_private_data_home_envs_first_named_variable_set_wins(
    overlay_root: Path,
) -> None:
    key = "repos.private_data_home_envs"
    both = _env(
        overlay_root,
        EXAMPLE_PRIVATE_DATA_HOME="/clones/first",
        EXAMPLE_PRIVATE_DATA_PATH="/clones/second",
    )
    assert resolve_clone_home(key, environ=both) == "/clones/first"
    second_only = _env(overlay_root, EXAMPLE_PRIVATE_DATA_PATH="/clones/second")
    assert resolve_clone_home(key, environ=second_only) == "/clones/second"


def test_overlay_keys_private_data_home_unset_without_sibling_refuses(
    overlay_root: Path, tmp_path: Path
) -> None:
    key = "repos.private_data_home_envs"
    with pytest.raises(NodeOverlayError, match=r"repos\.private_data_home_envs"):
        resolve_clone_home(
            key, environ=_env(overlay_root), registry_root=tmp_path / "registry"
        )
    (tmp_path / "repo-private-data").mkdir()
    assert resolve_clone_home(
        key, environ=_env(overlay_root), registry_root=tmp_path / "registry"
    ) == str(tmp_path / "repo-private-data")


def test_overlay_keys_knowledge_base_home_env_variable_then_sibling_then_refusal(
    overlay_root: Path, tmp_path: Path
) -> None:
    key = "repos.knowledge_base_home_env"
    with pytest.raises(NodeOverlayError, match=r"repos\.knowledge_base_home_env"):
        resolve_clone_home(
            key, environ=_env(overlay_root), registry_root=tmp_path / "registry"
        )
    named = _env(overlay_root, EXAMPLE_KNOWLEDGE_BASE_PATH="/clones/kb")
    assert resolve_clone_home(key, environ=named) == "/clones/kb"
    (tmp_path / "repo-knowledge-base").mkdir()
    assert resolve_clone_home(
        key, environ=_env(overlay_root), registry_root=tmp_path / "registry"
    ) == str(tmp_path / "repo-knowledge-base")


def test_overlay_keys_clone_home_refuses_a_key_that_names_no_variable() -> None:
    with pytest.raises(NodeOverlayError, match=r"repos\.deploy"):
        resolve_clone_home("repos.deploy", environ={})


def test_overlay_keys_invalid_overlay_is_a_refusal_not_an_absent_overlay(
    tmp_path: Path,
) -> None:
    documents = dict(FIXTURE_DOCUMENTS)
    documents["repos"] = {**FIXTURE_DOCUMENTS["repos"], "plugin_source": "has/slash"}
    root = tmp_path / "overlays"
    _write_overlays(root, documents)
    with pytest.raises(NodeOverlayError, match="plugin_source"):
        resolve_overlay_key("repos.plugin_source", environ=_env(root))


def test_overlay_keys_explicit_value_wins_and_is_validated(overlay_root: Path) -> None:
    env = _env(overlay_root)
    assert (
        resolve_overlay_key("repos.deploy", explicit="repo-other", environ=env)
        == "repo-other"
    )
    with pytest.raises(NodeOverlayError, match=r"repos\.deploy"):
        resolve_overlay_key("repos.deploy", explicit="has/slash", environ=env)
    with pytest.raises(NodeOverlayError, match=r"repos\.deploy"):
        resolve_overlay_key("repos.deploy", explicit="", environ=env)
