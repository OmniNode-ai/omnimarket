# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Builds installed fixture packages on disk for the node announcement tests (OMN-21597).

A package is a ``<name>-<version>.dist-info`` directory (metadata and entry points)
beside a module directory holding one ``contract.yaml`` per node. Nothing here is
importable and nothing is imported: the announcer reads metadata and files only.
"""

import shutil
from pathlib import Path

NODES_GROUP = "onex.nodes"


def contract_text(
    name: str,
    *,
    handler_module: str,
    profiles: tuple[str, ...] = (),
    newline: str = "\n",
    extra_topic: str | None = None,
) -> bytes:
    """Return the bytes of a small node contract, with the requested line endings."""
    lines = [
        f"name: {name}",
        "node_type: compute",
        "event_bus:",
        "  subscribe_topics:",
        f"    - onex.cmd.fixture.{name.replace('_', '-')}-requested.v1",
        "  publish_topics:",
        f"    - onex.evt.fixture.{name.replace('_', '-')}-completed.v1",
    ]
    if extra_topic is not None:
        lines.append(f"    - {extra_topic}")
    lines += [
        "handler_routing:",
        "  handlers:",
        "    - handler:",
        f"        module: {handler_module}",
        "        name: Handler",
    ]
    if profiles:
        lines.append("runtime_profiles:")
        lines += [f"  - {profile}" for profile in profiles]
    return (newline.join(lines) + newline).encode("utf-8")


def write_package(
    site: Path,
    distribution: str,
    version: str,
    contracts: dict[str, bytes],
) -> None:
    """Install a fixture distribution under ``site``.

    ``contracts`` maps a node's entry-point name to the bytes of its contract file.
    """
    module_root = distribution.replace("-", "_")
    dist_info = site / f"{module_root}-{version}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {version}\n",
        encoding="utf-8",
    )
    entry_lines = [f"[{NODES_GROUP}]"]
    for node_name, raw in contracts.items():
        package_dir = site / module_root / "nodes" / node_name
        package_dir.mkdir(parents=True)
        (package_dir / "contract.yaml").write_bytes(raw)
        entry_lines.append(f"{node_name} = {module_root}.nodes.{node_name}.node:Node")
    (dist_info / "entry_points.txt").write_text(
        "\n".join(entry_lines) + "\n", encoding="utf-8"
    )


def remove_package(site: Path, distribution: str, version: str) -> None:
    """Uninstall a fixture distribution."""
    module_root = distribution.replace("-", "_")
    shutil.rmtree(site / f"{module_root}-{version}.dist-info")
    shutil.rmtree(site / module_root)


def install_standard_site(site: Path) -> dict[str, bytes]:
    """Two packages, three contracts, one of them with Windows line endings.

    Returns the bytes written, keyed by contract name.
    """
    one = contract_text(
        "fixture_one",
        handler_module="alpha_pkg.nodes.fixture_one.handler",
        newline="\r\n",
    )
    two = contract_text(
        "fixture_two", handler_module="alpha_pkg.nodes.fixture_two.handler"
    )
    three = contract_text(
        "fixture_three", handler_module="beta_pkg.nodes.fixture_three.handler"
    )
    write_package(site, "alpha-pkg", "1.0.0", {"fixture_one": one, "fixture_two": two})
    write_package(site, "beta-pkg", "2.1.0", {"fixture_three": three})
    return {"fixture_one": one, "fixture_two": two, "fixture_three": three}
