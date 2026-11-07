# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: the other reasons a directory marketplace has no directory, the closed request and the failing control (OMN-22687)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_parse_probe_compute.handlers import (
    HandlerHookParseProbe,
)
from omnimarket.nodes.node_hook_parse_probe_compute.models import (
    ModelHookParseProbeRequest,
)
from tests.nodes.node_hook_parse_probe_compute.probe_world import (
    KEY,
    render,
    run_probe,
    volume_layout,
)

UNKNOWN = "HOOKPARSE unknown marketplace-unreadable:"


def test_a_missing_directory_off_the_volumes_root_names_its_path(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    home, market = volume_layout(tmp_path / "a", tmp_path / "elsewhere", "gone")
    line = run_probe(home, render(volumes_root=str(volumes)), tmp_path)
    assert line == f"{UNKNOWN}directory-source-missing-{market}"


def test_no_declared_volumes_root_names_the_path_and_never_guesses_a_volume(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    home, market = volume_layout(tmp_path, volumes, "OmniDataAbsent")
    line = run_probe(home, render(), tmp_path)
    assert line == f"{UNKNOWN}directory-source-missing-{market}"
    assert "volume-not-mounted" not in line


def test_a_mounted_volume_without_the_marketplace_directory_names_the_path(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / "volumes"
    (volumes / "OmniData").mkdir(parents=True)
    home, market = volume_layout(tmp_path, volumes, "OmniData")
    line = run_probe(home, render(volumes_root=str(volumes)), tmp_path)
    assert line == f"{UNKNOWN}directory-source-missing-{market}"


def test_a_mounted_volume_whose_manifest_lacks_the_plugin_keeps_the_manifest_reason(
    tmp_path: Path,
) -> None:
    volumes = tmp_path / "volumes"
    home, market = volume_layout(tmp_path, volumes, "OmniData")
    real = market.resolve()
    real.mkdir(parents=True)
    line = run_probe(home, render(volumes_root=str(volumes)), tmp_path)
    assert line == f"{UNKNOWN}no-directory-source-for-{KEY}"


def test_a_volumes_root_with_a_quote_is_carried_as_data(tmp_path: Path) -> None:
    volumes = tmp_path / 'vol"umes'
    volumes.mkdir()
    home, _ = volume_layout(tmp_path, volumes, "OmniDataAbsent")
    line = run_probe(home, render(volumes_root=str(volumes)), tmp_path)
    assert line == f"{UNKNOWN}volume-not-mounted-{volumes}/OmniDataAbsent"


def test_hook_trees_that_are_not_plain_relative_paths_are_dropped() -> None:
    result = HandlerHookParseProbe().handle(
        ModelHookParseProbeRequest(
            hook_trees=(
                "lab-run/plugins/a/",
                "../etc",
                "/abs",
                "a/../b",
                "x y",
                "lab-run/plugins/a",
            )
        )
    )
    assert result.hook_trees == ("lab-run/plugins/a",)
    assert 'python3 - /bin/bash "lab-run/plugins/a"' in result.script


@pytest.mark.parametrize("root", ["relative/vols", "/", "//", ""])
def test_a_volumes_root_that_is_not_an_absolute_directory_is_refused(root: str) -> None:
    with pytest.raises(ValidationError):
        ModelHookParseProbeRequest(volumes_root=root)


def test_the_request_is_closed_to_fields_it_does_not_declare() -> None:
    with pytest.raises(ValidationError):
        ModelHookParseProbeRequest.model_validate({"volumes": "x"})


def test_failing_control_without_the_directory_check_the_old_reason_comes_back(
    tmp_path: Path,
) -> None:
    """The named injected break: the script without its missing-directory check reads the recorded layout as the manifest fault
    the host printed on 2026-11-06, so the golden test reads the check and not the layout."""
    volumes = tmp_path / "volumes"
    volumes.mkdir()
    home, _ = volume_layout(tmp_path, volumes, "OmniDataAbsent")
    script = render(volumes_root=str(volumes))
    check = "    if not os.path.isdir(top):\n        raise ValueError(missing_reason(top))\n"
    assert check in script
    broken = script.replace(check, "")
    assert (
        run_probe(home, broken, tmp_path) == f"{UNKNOWN}no-directory-source-for-{KEY}"
    )
    assert run_probe(home, script, tmp_path) != run_probe(home, broken, tmp_path)


def test_the_rendered_script_holds_no_volume_path_of_its_own() -> None:
    assert "VOLUMES_ROOT = None" in render()
