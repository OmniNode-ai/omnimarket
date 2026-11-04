# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane census host registry is overlay data of the census node (OMN-22152).

The registry says which hosts exist for the census, the names and aliases each reports, and
the lanes each may host. The package ships the schema and a placeholder example; the values
come from the node overlay. Every host, alias and lane in these tests is a synthetic fixture
built in ``tmp_path``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.models.model_lane_census import (
    ModelLaneCensusRegistryRow,
    ModelLaneCensusScheduleOverlay,
)
from omnimarket.nodes.node_lane_census_effect import __main__ as node_main
from omnimarket.nodes.node_lane_census_effect.handlers import (
    adapter_lane_census_schedule as schedule_module,
)
from omnimarket.nodes.node_lane_census_effect.handlers.adapter_lane_census_schedule import (
    REGISTRY_ENV,
    LaneCensusScheduler,
    load_schedule,
    render_units,
    resolve_registry_path,
)

pytestmark = pytest.mark.unit

NODE_DIR = Path(schedule_module.__file__).resolve().parents[1]
HOST_ROW = {"name": "host-a", "scheduler": "systemd-user", "docker": "engine"}


def registry_row(
    name: str, aliases: Sequence[str] = (), lanes: Sequence[str] = ()
) -> dict[str, object]:
    return {"name": name, "aliases": list(aliases), "lanes": list(lanes)}


def overlay_document(*registry: dict[str, object]) -> dict[str, object]:
    return {"hosts": [dict(HOST_ROW)], "registry": list(registry)}


def good_registry() -> tuple[dict[str, object], ...]:
    return (
        registry_row("host-a", ["node-a"], ["lane-one", "lane-two"]),
        registry_row("host-b", ["node-b"], ["lane-two"]),
        registry_row("host-c", ["node-c"], []),
    )


def write_overlay(root: Path, document: Mapping[str, object]) -> Path:
    target = root / schedule_module.NODE_NAME / "overlay.yaml"
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(dict(document)), encoding="utf-8")
    return target


def test_registry_accepts_a_valid_overlay_and_example_validates() -> None:
    overlay = ModelLaneCensusScheduleOverlay.model_validate(
        overlay_document(*good_registry())
    )
    assert [row.name for row in overlay.registry] == ["host-a", "host-b", "host-c"]
    example = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text("utf-8"))
    parsed = ModelLaneCensusScheduleOverlay.model_validate(example)
    assert parsed.registry, "positive control: the example carries registry rows"
    assert parsed.hosts, "positive control: the example carries hosts rows"


def test_registry_refuses_an_alias_claimed_by_two_hosts() -> None:
    document = overlay_document(
        registry_row("host-a", ["node-a"]), registry_row("host-b", ["node-a"])
    )
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(document)
    # Matching is on the lowercased first DNS label, so these claim one host name twice.
    spelled = overlay_document(
        registry_row("host-a", ["Node-A"]),
        registry_row("host-b", ["node-a.example.test"]),
    )
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(spelled)
    # Positive control: distinct aliases validate.
    ModelLaneCensusScheduleOverlay.model_validate(
        overlay_document(
            registry_row("host-a", ["node-a"]), registry_row("host-b", ["node-b"])
        )
    )


def test_registry_refuses_the_same_alias_twice_on_one_host() -> None:
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(registry_row("host-a", ["node-a", "NODE-A"]))
        )


def test_registry_refuses_an_alias_that_identifies_no_single_host() -> None:
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(registry_row("host-a", ["docker-desktop"]))
        )
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(registry_row("host-a", ["Docker-Desktop"]))
        )


def test_registry_refuses_an_empty_name_and_an_empty_alias() -> None:
    for name in ("", "   "):
        with pytest.raises(ValidationError):
            ModelLaneCensusScheduleOverlay.model_validate(
                overlay_document(registry_row(name, ["node-a"]))
            )
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(registry_row("host-a", [""]))
        )


def test_registry_refuses_a_host_listed_twice() -> None:
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(
                registry_row("host-a", ["node-a"]), registry_row("host-a", ["node-b"])
            )
        )


def test_registry_row_has_no_defaults_and_forbids_extras() -> None:
    for missing in ("name", "aliases", "lanes"):
        document = registry_row("host-a", ["node-a"], ["lane-one"])
        del document[missing]
        with pytest.raises(ValidationError):
            ModelLaneCensusRegistryRow.model_validate(document)
    with pytest.raises(ValidationError):
        ModelLaneCensusRegistryRow.model_validate(
            {**registry_row("host-a"), "address": "192.0.2.1"}
        )
    # The overlay carries no registry by default either.
    with pytest.raises(ValidationError):
        ModelLaneCensusScheduleOverlay.model_validate({"hosts": [dict(HOST_ROW)]})


def test_rendered_unit_carries_the_registry_path() -> None:
    service, _timer = render_units(
        load_schedule(),
        omni_root=Path("/srv/omni"),
        venv=Path("/srv/omni/.onex-dispatch-venv"),
        home=Path("/home/u"),
        registry_path=Path("/srv/overlays/node_lane_census_effect/overlay.yaml"),
    )
    assert (
        f'Environment="{REGISTRY_ENV}=/srv/overlays/node_lane_census_effect/overlay.yaml"'
    ) in service


def test_install_renders_the_resolved_overlay_path_into_the_unit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlays"
    path = write_overlay(root, overlay_document(*good_registry()))
    omni = tmp_path / "omni"
    scheduler = LaneCensusScheduler(
        schedule=load_schedule(),
        overlay=ModelLaneCensusScheduleOverlay.model_validate(
            overlay_document(*good_registry())
        ),
        home=tmp_path / "home",
        environ={OVERLAY_ROOTS_ENV: str(root)},
    )
    result = scheduler.install("host-a", omni_root=omni, dry_run=True)
    assert result.exit_code == 0, result.stderr
    assert f'Environment="{REGISTRY_ENV}={path.absolute()}"' in result.stdout


def test_install_refuses_when_no_root_supplies_the_overlay(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    omni = tmp_path / "omni"
    python = omni / ".onex-dispatch-venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    overlay = ModelLaneCensusScheduleOverlay.model_validate(
        overlay_document(*good_registry())
    )
    for environ in ({}, {OVERLAY_ROOTS_ENV: str(tmp_path / "empty-root")}):
        scheduler = LaneCensusScheduler(
            schedule=load_schedule(),
            overlay=overlay,
            home=tmp_path / "home",
            run=run,
            environ=environ,
        )
        for dry_run in (False, True):
            result = scheduler.install("host-a", omni_root=omni, dry_run=dry_run)
            assert result.exit_code == 1
            assert OVERLAY_ROOTS_ENV in result.stderr
    assert calls == []
    assert not (tmp_path / "home").exists()


def test_resolve_registry_path_takes_the_first_root_that_holds_one(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_overlay(second, overlay_document(*good_registry()))
    environ = {OVERLAY_ROOTS_ENV: f"{first}:{second}"}
    assert resolve_registry_path(environ) == (
        second / schedule_module.NODE_NAME / "overlay.yaml"
    )
    write_overlay(first, overlay_document(*good_registry()))
    assert resolve_registry_path(environ) == (
        first / schedule_module.NODE_NAME / "overlay.yaml"
    )
    with pytest.raises(NodeOverlayError, match=OVERLAY_ROOTS_ENV):
        resolve_registry_path({})


def test_registry_path_prints_the_path_and_fails_with_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "overlays"
    path = write_overlay(root, overlay_document(*good_registry()))
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(root))
    assert node_main.main(["registry", "path"]) == 0
    printed = capsys.readouterr()
    assert printed.out.strip() == str(path.absolute())
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(tmp_path / "empty-root"))
    assert node_main.main(["registry", "path"]) != 0
    refused = capsys.readouterr()
    assert refused.out == ""
    assert OVERLAY_ROOTS_ENV in refused.err
    monkeypatch.delenv(OVERLAY_ROOTS_ENV)
    assert node_main.main(["registry", "path"]) != 0


def test_schedule_check_fails_for_an_invalid_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    valid = tmp_path / "valid"
    write_overlay(valid, overlay_document(*good_registry()))
    monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(valid))
    assert node_main.main(["schedule", "check"]) == 0, capsys.readouterr().err
    capsys.readouterr()
    for number, registry in enumerate(
        (
            (registry_row("host-a", ["node-a"]), registry_row("host-b", ["node-a"])),
            (registry_row("host-a", ["docker-desktop"]),),
            (registry_row("", ["node-a"]),),
            (registry_row("host-a", ["node-a"]), registry_row("host-a", ["node-b"])),
        )
    ):
        broken = tmp_path / f"broken-{number}"
        write_overlay(broken, overlay_document(*registry))
        monkeypatch.setenv(OVERLAY_ROOTS_ENV, str(broken))
        assert node_main.main(["schedule", "check"]) != 0
        assert "registry" in capsys.readouterr().err
