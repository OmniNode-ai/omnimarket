# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Strict deployment overlay precedence and schema validation."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_disk_hygiene_effect.handlers.handler_lab_disk_hygiene import (
    OVERLAY_ENV,
    HandlerLabDiskHygiene,
    LabDiskHygieneConfigurationError,
    load_deployment_overlay,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models import (
    ModelLabDiskHygieneDeployment,
)
from omnimarket.nodes.node_lab_disk_hygiene_effect.models.model_lab_disk_hygiene_request import (
    ModelLabDiskHygieneRequest,
)

from .test_lab_disk_hygiene_effect import CONTAINERS, FakeDocker, _census

FIRST = {"census_path": "first.yaml"}
SECOND = {"census_path": "second.yaml"}


@pytest.fixture(autouse=True)
def clear_overlay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


def _write(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_pointer_wins_over_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pointer = _write(tmp_path / "explicit.yaml", FIRST)
    _write(tmp_path / "root" / "node_lab_disk_hygiene_effect" / "overlay.yaml", SECOND)
    monkeypatch.setenv(OVERLAY_ENV, str(pointer))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "root"))
    deployment = load_deployment_overlay()
    assert isinstance(deployment, ModelLabDiskHygieneDeployment)
    assert deployment == _first()


def test_roots_use_first_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first" / "node_lab_disk_hygiene_effect" / "overlay.yaml"
    _write(first, FIRST)
    _write(
        tmp_path / "second" / "node_lab_disk_hygiene_effect" / "overlay.yaml", SECOND
    )
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join(
            (
                "",
                str(tmp_path / "missing"),
                str(tmp_path / "first"),
                "",
                str(tmp_path / "second"),
            )
        ),
    )
    assert load_deployment_overlay() == _first()
    first.unlink()
    assert load_deployment_overlay() == _second()


@pytest.mark.parametrize("pointer", ["missing.yaml", ""])
def test_set_but_missing_pointer_refuses_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pointer: str
) -> None:
    _write(tmp_path / "root" / "node_lab_disk_hygiene_effect" / "overlay.yaml", FIRST)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "root"))
    monkeypatch.setenv(OVERLAY_ENV, str(tmp_path / pointer) if pointer else "")
    with pytest.raises(LabDiskHygieneConfigurationError, match=OVERLAY_ENV):
        load_deployment_overlay()


@pytest.mark.parametrize(
    "text",
    [
        "key: [",
        "unexpected: true",
        "null",
        "[]",
        "census_path: 7",
        "census_path: null",
        "census_path: []",
        "census_path: true",
        "census_path: ''",
        yaml.safe_dump({**FIRST, "unexpected": True}),
    ],
)
def test_invalid_yaml_keys_and_types_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    path = tmp_path / "overlay.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(OVERLAY_ENV, str(path))
    with pytest.raises(LabDiskHygieneConfigurationError, match=OVERLAY_ENV):
        load_deployment_overlay()


def test_invalid_first_root_refuses_later_valid_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(
        tmp_path / "first" / "node_lab_disk_hygiene_effect" / "overlay.yaml",
        {"unknown": True},
    )
    _write(tmp_path / "second" / "node_lab_disk_hygiene_effect" / "overlay.yaml", FIRST)
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS",
        os.pathsep.join((str(tmp_path / "first"), str(tmp_path / "second"))),
    )
    with pytest.raises(
        LabDiskHygieneConfigurationError, match="ONEX_SKILL_OVERLAY_ROOTS"
    ):
        load_deployment_overlay()


def test_unreadable_pointer_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(OVERLAY_ENV, str(tmp_path))
    with pytest.raises(LabDiskHygieneConfigurationError, match=OVERLAY_ENV):
        load_deployment_overlay()


def test_no_overlay_resolves_none() -> None:
    assert load_deployment_overlay() is None


def _first() -> ModelLabDiskHygieneDeployment:
    return ModelLabDiskHygieneDeployment("first.yaml")


def _second() -> ModelLabDiskHygieneDeployment:
    return ModelLabDiskHygieneDeployment("second.yaml")


def test_deployment_is_frozen() -> None:
    with pytest.raises(FrozenInstanceError):
        _first().census_path = "other.yaml"


@pytest.mark.parametrize("value", [7, None, "", " ", ["census.yaml"]])
def test_injected_deployment_types_are_validated(value: object) -> None:
    with pytest.raises(ValueError, match="census_path"):
        ModelLabDiskHygieneDeployment(**{"census_path": value})


def test_no_census_preserves_containers_and_runs_prunes(tmp_path: Path) -> None:
    docker = FakeDocker(CONTAINERS)
    result = HandlerLabDiskHygiene(
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "docker",
        held_paths=set,
    ).handle(ModelLabDiskHygieneRequest(omni_home=tmp_path))
    assert result.removed_containers == ()
    assert (
        "census not configured: no request census and no overlay census_path; no container removed"
        in result.errors
    )
    assert not any(call[1] == "rm" for call in docker.calls)
    assert {step.step for step in result.steps} == {"images", "build_cache", "volumes"}


@pytest.mark.parametrize("absolute", [False, True])
def test_census_path_resolves_against_request_workspace(
    tmp_path: Path, absolute: bool
) -> None:
    census = _census(tmp_path)
    deployment = ModelLabDiskHygieneDeployment(str(census) if absolute else census.name)
    docker = FakeDocker(CONTAINERS)
    result = HandlerLabDiskHygiene(
        overlay=deployment,
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "docker",
        held_paths=set,
    ).handle(ModelLabDiskHygieneRequest(omni_home=tmp_path))
    assert not result.errors
    assert set(result.removed_containers) == {"landing-l9-pg", "rlane-x-pg"}


def test_request_census_wins_over_injected_overlay(tmp_path: Path) -> None:
    docker = FakeDocker(CONTAINERS)
    result = HandlerLabDiskHygiene(
        overlay=_first(),
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "docker",
        held_paths=set,
    ).handle(ModelLabDiskHygieneRequest(omni_home=tmp_path, census=_census(tmp_path)))
    assert not result.errors
    assert result.removed_containers


def test_overlay_is_resolved_at_handle_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker(CONTAINERS)
    handler = HandlerLabDiskHygiene(
        run=docker.run,
        disk_free=docker.disk_free,
        which=lambda _: "docker",
        held_paths=set,
    )
    pointer = _write(tmp_path / "overlay.yaml", {"census_path": _census(tmp_path).name})
    monkeypatch.setenv(OVERLAY_ENV, str(pointer))
    assert handler.handle(
        ModelLabDiskHygieneRequest(omni_home=tmp_path)
    ).removed_containers
