# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab host table is read from the overlays repository through the overlay roots, never from the config owner's copy.

Measured on the operator Mac (2026-11-12): the host installer read the config owner's copy of the table
(last changed 2026-11-07, its Mac row declaring no clones) instead of the overlays repository's table that
declares the Mac's clone, so the default install refused. Two shapes reached the stale copy: the retired
copy listed among the overlay roots, and the workspace-owner discovery that was tried when no root held
the table. Both are refused here; the overlays repository's root, listed anywhere, serves the table.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import (
    OVERLAY_ROOTS_ENV,
    NodeOverlayError,
)
from omnimarket.handlers.overlay_keys import (
    CONFIG_ROOT_ENV,
    LAB_HOSTS_FILE_ENV,
    resolve_lab_hosts_table,
)
from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    default_hosts,
)

pytestmark = pytest.mark.unit

RETIRED_PARTS = ("config", "onex", "overlays")


def write_table(root: Path, name: str) -> Path:
    """Write ``<root>/lab_hosts/overlay.yaml`` with one host named ``name``; return ``root``."""
    path = root / "lab_hosts" / "overlay.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "hosts": [{"name": name, "target": "192.0.2.10"}],
                "registry_root_candidates": ["/srv/example/registry"],
            }
        ),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def both_copies(tmp_path: Path) -> dict[str, Path]:
    """The overlays repository's overlays directory and the config owner's retired copy, each with its own host."""
    config = tmp_path / "config-owner"
    return {
        "config": config,
        "overlays": write_table(
            tmp_path / "overlays-repo" / "overlays", "from-overlays"
        ),
        "retired": write_table(config.joinpath(*RETIRED_PARTS), "from-config-owner"),
    }


def names(env: dict[str, str]) -> list[str]:
    return [h.name for h in resolve_lab_hosts_table(env).hosts]


def test_the_overlays_repository_serves_the_table_whatever_the_root_order(
    both_copies: dict[str, Path],
) -> None:
    retired, overlays = both_copies["retired"], both_copies["overlays"]
    for roots in ((retired, overlays), (overlays, retired)):
        env = {
            OVERLAY_ROOTS_ENV: ":".join(str(r) for r in roots),
            CONFIG_ROOT_ENV: str(both_copies["config"]),
        }
        assert names(env) == ["from-overlays"]


def test_the_config_owner_copy_alone_is_refused_not_read(
    both_copies: dict[str, Path], tmp_path: Path
) -> None:
    config = str(both_copies["config"])
    listed = {OVERLAY_ROOTS_ENV: str(both_copies["retired"]), CONFIG_ROOT_ENV: config}
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.hosts"):
        resolve_lab_hosts_table(listed)
    # The old fallback: no root holds the table, so the workspace owner's overlays were searched.
    unlisted = {
        OVERLAY_ROOTS_ENV: str(tmp_path / "plugin-overlays"),
        CONFIG_ROOT_ENV: config,
    }
    with pytest.raises(NodeOverlayError, match=r"never from the config owner's copy"):
        resolve_lab_hosts_table(unlisted)
    # The developer workspace-config copy under the registry root is the same copy.
    developer = write_table(
        tmp_path / "home" / ".onex" / "workspace-config" / Path(*RETIRED_PARTS),
        "from-developer",
    )
    assert developer.is_dir()
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.hosts"):
        resolve_lab_hosts_table({"OMNI_HOME": str(tmp_path / "home")})


def test_the_pinned_file_still_wins(
    both_copies: dict[str, Path], tmp_path: Path
) -> None:
    pinned = (
        write_table(tmp_path / "pinned", "from-pinned") / "lab_hosts" / "overlay.yaml"
    )
    env = {
        LAB_HOSTS_FILE_ENV: str(pinned),
        OVERLAY_ROOTS_ENV: str(both_copies["overlays"]),
        CONFIG_ROOT_ENV: str(both_copies["config"]),
    }
    assert names(env) == ["from-pinned"]


def test_the_installer_default_reads_the_overlays_repository(
    both_copies: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer's default host source, the reader the Mac refused through, serves the overlays repository's table."""
    monkeypatch.delenv(LAB_HOSTS_FILE_ENV, raising=False)
    monkeypatch.setenv(CONFIG_ROOT_ENV, str(both_copies["config"]))
    monkeypatch.setenv(
        OVERLAY_ROOTS_ENV, f"{both_copies['retired']}:{both_copies['overlays']}"
    )
    assert [h.name for h in default_hosts().hosts] == ["from-overlays"]
